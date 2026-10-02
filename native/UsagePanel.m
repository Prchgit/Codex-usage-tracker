#import <Cocoa/Cocoa.h>

#import "ScreenDetection.h"

static const NSTimeInterval DefaultPollIntervalSeconds = 2;
static const NSTimeInterval RequestTimeoutSeconds = 4;
static NSString * const ShowTrackerNotification = @"com.local.codex-usage-panel.show";
static const CGFloat PanelWidth = 340;
static const CGFloat SessionRowHeight = 80;
static const CGFloat HeaderHeight = 52;
static NSString * const DefaultDashboardURL = @"http://127.0.0.1:8767/";

static NSString *AccountUsageText(NSDictionary *snapshot) {
    NSArray *limits = [snapshot[@"limits"] isKindOfClass:NSArray.class] ? snapshot[@"limits"] : @[];
    NSMutableArray *codex = [NSMutableArray new];
    for (NSDictionary *limit in limits) {
        if ([limit isKindOfClass:NSDictionary.class] && [limit[@"limit_id"] isEqual:@"codex"]) [codex addObject:limit];
    }
    if (codex.count) limits = codex;
    NSMutableArray *parts = [NSMutableArray new];
    for (NSDictionary *limit in limits) {
        if (![limit isKindOfClass:NSDictionary.class] || ![limit[@"used_percent"] isKindOfClass:NSNumber.class]) continue;
        NSString *label = limits.count > 1 && [limit[@"label"] isKindOfClass:NSString.class] ? [limit[@"label"] stringByAppendingString:@" "] : @"";
        [parts addObject:[NSString stringWithFormat:@"%@%.0f%%", label, [limit[@"used_percent"] doubleValue]]];
    }
    if (!parts.count) return @"Usage unavailable";
    return [NSString stringWithFormat:@"%@ used%@", [parts componentsJoinedByString:@" · "], [snapshot[@"stale"] boolValue] ? @" (stale)" : @""];
}

// Native presentation only; usage is read from the existing loopback collector.
@interface UsagePanel : NSPanel @end
@implementation UsagePanel
- (BOOL)canBecomeKeyWindow { return NO; }
@end
@interface FlippedView : NSView @end
@implementation FlippedView
- (BOOL)isFlipped { return YES; }
@end
@interface App : NSObject <NSApplicationDelegate>
@property UsagePanel *panel;
@property NSStatusItem *item;
@property NSTimer *timer;
@property BOOL pending, manuallyHidden, collapsed;
@property NSArray *lastSessions;
@property NSString *lastMessage;
@property NSPoint savedScroll;
@property NSMutableDictionary *chatNumbers;
@property NSString *lastSignature;
@property NSURL *dashboardURL;
@property NSTimeInterval pollInterval;
@property NSDictionary *accountUsage;
@end
@implementation App
- (NSTextField *)label:(CGFloat)size weight:(NSFontWeight)weight color:(NSColor *)color frame:(NSRect)frame view:(NSView *)view {
    NSTextField *label = [NSTextField labelWithString:@""];
    label.font = [NSFont systemFontOfSize:size weight:weight];
    label.textColor = color; label.lineBreakMode = NSLineBreakByTruncatingTail;
    label.frame = frame;
    [view addSubview:label];
    return label;
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSDictionary *environment = NSProcessInfo.processInfo.environment;
    NSURL *url = [NSURL URLWithString:environment[@"CUT_DASHBOARD_URL"] ?: DefaultDashboardURL];
    if (![url.scheme isEqualToString:@"http"] ||
        ![@[@"127.0.0.1", @"localhost"] containsObject:url.host] || url.user || url.password ||
        ![url.path isEqualToString:@"/"] || url.query || url.fragment) {
        NSLog(@"Invalid CUT_DASHBOARD_URL; using default loopback dashboard");
        url = [NSURL URLWithString:DefaultDashboardURL];
    }
    self.dashboardURL = url;
    NSString *interval = environment[@"CUT_POLL_INTERVAL"];
    NSScanner *scanner = [NSScanner scannerWithString:interval ?: @""];
    double seconds;
    BOOL valid = [scanner scanDouble:&seconds] && scanner.isAtEnd && isfinite(seconds) && seconds > 0;
    self.pollInterval = valid ? seconds : DefaultPollIntervalSeconds;
    if (interval && !valid) NSLog(@"Invalid CUT_POLL_INTERVAL; using default polling interval");
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    self.item = [NSStatusBar.systemStatusBar statusItemWithLength:NSVariableStatusItemLength];
    self.item.button.title = @"◉ Tracker";
    NSMenu *menu = [NSMenu new];
    for (NSArray *entry in @[@[@"Show / hide floating usage", NSStringFromSelector(@selector(toggle:))],
                             @[@"Open usage history", NSStringFromSelector(@selector(openHistory:))],
                             @[@"Quit floating view", NSStringFromSelector(@selector(quit:))]]) {
        NSMenuItem *item = [menu addItemWithTitle:entry[0] action:NSSelectorFromString(entry[1]) keyEquivalent:@""];
        item.target = self;
    }
    self.item.menu = menu;
    self.panel = [[UsagePanel alloc] initWithContentRect:NSMakeRect(0,0,PanelWidth,154)
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
    self.panel.title = @"Codex Usage Tracker";
    self.panel.level = NSFloatingWindowLevel;
    self.panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    self.panel.movableByWindowBackground = YES;
    self.panel.releasedWhenClosed = NO; self.panel.hidesOnDeactivate = NO;
    self.panel.opaque = NO; self.panel.backgroundColor = NSColor.clearColor; self.panel.hasShadow = YES;
    [self.panel setFrameAutosaveName:@"CodexUsagePanel"];
    if (![self.panel setFrameUsingName:@"CodexUsagePanel"]) {
        NSRect f = NSScreen.mainScreen.visibleFrame;
        [self.panel setFrameOrigin:NSMakePoint(NSMaxX(f)-364, NSMinY(f)+28)];
    }
    self.chatNumbers = [NSMutableDictionary new];
    [self render:@[] message:@"Connecting to local monitor"];
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(syncVisibility:)
        name:NSWorkspaceDidActivateApplicationNotification object:nil];
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(show:)
        name:ShowTrackerNotification object:nil];
    [self syncVisibility:nil]; [self refresh:nil];
    self.timer = [NSTimer scheduledTimerWithTimeInterval:self.pollInterval target:self selector:@selector(refresh:) userInfo:nil repeats:YES];
}
- (void)syncVisibility:(id)sender {
    NSRunningApplication *foreground = NSWorkspace.sharedWorkspace.frontmostApplication;
    BOOL show = ShouldShowPanel(foreground.bundleIdentifier, self.manuallyHidden);
    BOOL changed = show != self.panel.visible;
    if (show) { if (!self.panel.visible) [self.panel orderFrontRegardless]; }
    else [self.panel orderOut:nil];
    if (changed) NSLog(@"Floating visibility: %d (Codex / ChatGPT Work foreground)",show);
}
- (void)show:(id)sender {
    self.manuallyHidden = NO;
    [self syncVisibility:nil];
}
- (void)toggle:(id)sender {
    if (!self.panel.visible) { [self show:sender]; return; }
    self.manuallyHidden = YES;
    [self syncVisibility:nil];
}
- (void)toggleCollapsed:(id)sender {
    self.collapsed = !self.collapsed;
    [self render:self.lastSessions ?: @[] message:self.lastMessage];
}
- (void)openHistory:(id)sender { [NSWorkspace.sharedWorkspace openURL:self.dashboardURL]; }
- (void)quit:(id)sender { [NSApp terminate:nil]; }
- (NSString *)number:(id)value {
    if (![value isKindOfClass:NSNumber.class]) return @"—";
    return [NSNumberFormatter localizedStringFromNumber:value numberStyle:NSNumberFormatterDecimalStyle];
}
- (NSString *)creditText:(NSDictionary *)turn {
    id credits = turn[@"estimated_credits"];
    BOOL partial = ![turn[@"credit_coverage_complete"] boolValue];
    if (partial && [turn[@"credit_priced_calls"] integerValue] > 0) credits = turn[@"known_estimated_credits"];
    NSString *footer = @"Credits unavailable";
    if ([credits isKindOfClass:NSString.class]) {
        NSNumberFormatter *f = [NSNumberFormatter new]; f.numberStyle = NSNumberFormatterDecimalStyle;
        f.minimumFractionDigits = 2; f.maximumFractionDigits = 3;
        footer = [NSString stringWithFormat:@"Credits = ≈%@%@",[f stringFromNumber:[NSDecimalNumber decimalNumberWithString:credits]],partial ? @" · partial" : @""];
    }
    return footer;
}
- (void)render:(NSArray *)sessions message:(NSString *)message {
    self.lastSessions = sessions;
    self.lastMessage = message;
    NSPoint previousScroll = self.savedScroll;
    for (NSView *child in self.panel.contentView.subviews) {
        if ([child isKindOfClass:NSScrollView.class]) previousScroll = ((NSScrollView *)child).documentVisibleRect.origin;
    }
    self.savedScroll = previousScroll;
    NSRect screen = (self.panel.screen ?: NSScreen.mainScreen).visibleFrame;
    CGFloat height = self.collapsed ? HeaderHeight : MIN(HeaderHeight + MAX(1, sessions.count)*SessionRowHeight, MAX(132, screen.size.height-80));
    NSRect frame = self.panel.frame;
    frame.origin.y += frame.size.height-height; frame.size.height = height;
    frame.origin.y = MAX(NSMinY(screen), MIN(frame.origin.y, NSMaxY(screen)-height));
    [self.panel setFrame:frame display:YES];
    NSVisualEffectView *view = [[NSVisualEffectView alloc] initWithFrame:NSMakeRect(0,0,PanelWidth,height)];
    view.material = NSVisualEffectMaterialHUDWindow; view.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    view.state = NSVisualEffectStateActive; view.wantsLayer = YES; view.layer.cornerRadius = 16;
    self.panel.contentView = view;
    NSString *heading = [@"Codex Usage Tracker · " stringByAppendingString:AccountUsageText(self.accountUsage)];
    NSTextField *title = [self label:13 weight:NSFontWeightSemibold color:NSColor.labelColor frame:NSMakeRect(18,height-33,252,18) view:view];
    title.stringValue = heading;
    NSString *refresh = [self.accountUsage[@"auto_refresh"] boolValue]
        ? [NSString stringWithFormat:@"Automatic refresh every %@ seconds.", self.accountUsage[@"refresh_interval_seconds"]]
        : @"Manual snapshot; automatic refresh unavailable.";
    title.toolTip = [NSString stringWithFormat:@"Account-wide Codex usage. Updated: %@. %@ Separate from per-chat estimated credits.", self.accountUsage[@"updated_at"] ?: @"unavailable", refresh];
    for (CGFloat size = 13; size >= 10; size -= 0.5) {
        title.font = [NSFont systemFontOfSize:size weight:NSFontWeightSemibold];
        if ([heading sizeWithAttributes:@{NSFontAttributeName:title.font}].width <= title.frame.size.width) break;
    }
    NSButton *minimize = [NSButton buttonWithTitle:self.collapsed ? @"＋" : @"−" target:self action:@selector(toggleCollapsed:)];
    minimize.bordered = NO;
    minimize.frame = NSMakeRect(273,height-36,25,25);
    minimize.toolTip = self.collapsed ? @"Expand usage panel" : @"Minimize to header";
    [minimize setAccessibilityLabel:minimize.toolTip];
    [view addSubview:minimize];
    [title addGestureRecognizer:[[NSClickGestureRecognizer alloc] initWithTarget:self action:@selector(openHistory:)]];
    NSButton *hide = [NSButton buttonWithTitle:@"×" target:self action:@selector(toggle:)];
    hide.bordered = NO; hide.frame = NSMakeRect(299,height-36,28,25); [view addSubview:hide];
    if (self.collapsed) return;
    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSMakeRect(0,8,PanelWidth,height-HeaderHeight)];
    scroll.drawsBackground = NO; scroll.hasVerticalScroller = YES; scroll.autohidesScrollers = YES;
    FlippedView *list = [[FlippedView alloc] initWithFrame:NSMakeRect(0,0,324,MAX(1,sessions.count)*SessionRowHeight)];
    scroll.documentView = list; [view addSubview:scroll];
    NSMutableArray *logRows = [NSMutableArray new];
    if (!sessions.count) {
        NSTextField *empty = [self label:13 weight:NSFontWeightRegular color:NSColor.secondaryLabelColor frame:NSMakeRect(18,18,304,24) view:list];
        empty.stringValue = message ?: @"Waiting for a Codex task";
    }
    NSUInteger index = 0;
    for (NSDictionary *turn in sessions) {
        if (![turn isKindOfClass:NSDictionary.class]) continue;
        NSString *key = [turn[@"thread_id"] isKindOfClass:NSString.class] ? turn[@"thread_id"] : turn[@"turn_id"];
        if (![key isKindOfClass:NSString.class]) continue;
        if (!self.chatNumbers[key]) self.chatNumbers[key] = @(self.chatNumbers.count+1);
        NSString *model = [turn[@"model"] isKindOfClass:NSString.class] ? turn[@"model"] : @"Unknown model";
        NSString *effort = [turn[@"reasoning_effort"] isKindOfClass:NSString.class] ? turn[@"reasoning_effort"] : @"effort unavailable";
        model = [model stringByAppendingFormat:@" - %@",effort];
        NSString *status = [turn[@"status"] isKindOfClass:NSString.class] ? turn[@"status"] : @"observed";
        NSDictionary *usage = [turn[@"usage"] isKindOfClass:NSDictionary.class] ? turn[@"usage"] : @{};
        NSString *name = [turn[@"chat_name"] isKindOfClass:NSString.class] && [turn[@"chat_name"] length] ? turn[@"chat_name"] : [NSString stringWithFormat:@"Chat %@",self.chatNumbers[key]];
        NSString *state = [NSString stringWithFormat:@"%@ · %@ · %@ turns",model,status,[self number:turn[@"recorded_turns"]]];
        NSString *tokens = [NSString stringWithFormat:@"New %@ · Cache %@ · Out %@",[self number:usage[@"new_input_tokens"]],[self number:usage[@"cached_input_tokens"]],[self number:usage[@"output_tokens"]]];
        NSString *footer = [self creditText:turn];
        CGFloat y = index++*SessionRowHeight;
        CGFloat creditWidth = MIN(210, [footer sizeWithAttributes:@{NSFontAttributeName:[NSFont systemFontOfSize:11 weight:NSFontWeightMedium]}].width + 4);
        NSTextField *nameLabel = [self label:12 weight:NSFontWeightSemibold color:NSColor.labelColor frame:NSMakeRect(18,y+2,294-creditWidth,18) view:list];
        nameLabel.stringValue = name; nameLabel.toolTip = name;
        NSTextField *creditLabel = [self label:11 weight:NSFontWeightMedium color:NSColor.labelColor frame:NSMakeRect(322-creditWidth,y+2,creditWidth,18) view:list];
        creditLabel.stringValue = footer;
        creditLabel.toolTip = @"Cumulative estimated credits across completed or interrupted turns. The previous total is held while the current turn runs. Partial means some calls or turns could not be measured/priced. Assumes published Standard-speed rates; not account-reported consumption.";
        NSTextField *stateLabel = [self label:11 weight:NSFontWeightRegular color:NSColor.secondaryLabelColor frame:NSMakeRect(18,y+23,304,18) view:list];
        stateLabel.stringValue = state; stateLabel.toolTip = state;
        for (CGFloat size=11; size>=9; size-=0.5) {
            stateLabel.font = [NSFont systemFontOfSize:size];
            if ([state sizeWithAttributes:@{NSFontAttributeName:stateLabel.font}].width <= 304) break;
        }
        NSTextField *tokenLabel = [self label:11 weight:NSFontWeightRegular color:NSColor.secondaryLabelColor frame:NSMakeRect(18,y+44,304,22) view:list];
        tokenLabel.stringValue = tokens; tokenLabel.toolTip = [tokens stringByAppendingString:@" · Cumulative recorded chat usage"];
        for (CGFloat size = 11; size >= 9; size -= 0.5) {
            tokenLabel.font = [NSFont monospacedDigitSystemFontOfSize:size weight:NSFontWeightRegular];
            if ([tokens sizeWithAttributes:@{NSFontAttributeName:tokenLabel.font}].width <= 304) break;
        }
        [logRows addObject:[NSString stringWithFormat:@"%@; %@; %@; %@",name,state,tokens,footer]];
    }
    [list scrollPoint:previousScroll];
    [list addGestureRecognizer:[[NSClickGestureRecognizer alloc] initWithTarget:self action:@selector(openHistory:)]];
    NSString *signature = [NSString stringWithFormat:@"%@|%@",heading,message ?: [logRows componentsJoinedByString:@" | "]];
    if (![signature isEqual:self.lastSignature]) {
        self.lastSignature = signature;
        NSLog(@"Floating display: visible=%d; %@",self.panel.visible,signature);
    }
}
- (void)refresh:(id)sender {
    [self syncVisibility:nil];
    if (self.pending) return; self.pending = YES;
    NSURLRequest *request = [NSURLRequest requestWithURL:[NSURL URLWithString:@"api/turns" relativeToURL:self.dashboardURL]
        cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:RequestTimeoutSeconds];
    [[NSURLSession.sharedSession dataTaskWithRequest:request completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        id report = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            self.pending = NO;
            if(error || ![response isKindOfClass:NSHTTPURLResponse.class] || ((NSHTTPURLResponse *)response).statusCode != 200 ||
                ![report isKindOfClass:NSDictionary.class] || ![report[@"turns"] isKindOfClass:NSArray.class]) {
                self.accountUsage = nil;
                [self render:@[] message:@"Monitor offline · reconnecting"]; return;
            }
            self.accountUsage = [report[@"account_usage"] isKindOfClass:NSDictionary.class] ? report[@"account_usage"] : nil;
            NSArray *sessions = [report[@"floating_sessions"] isKindOfClass:NSArray.class] ? report[@"floating_sessions"] : @[];
            [self render:sessions message:sessions.count ? nil : @"Waiting for a Codex task"];

        });
    }] resume];
}
@end
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc > 1 && strcmp(argv[1], "--show") == 0) {
            [NSDistributedNotificationCenter.defaultCenter postNotificationName:ShowTrackerNotification object:nil
                userInfo:nil deliverImmediately:YES];
            puts("Requested tracker display while Codex / ChatGPT Work is foreground");
            return 0;
        }
        if (argc > 1 && strcmp(argv[1], "--self-test") == 0) {
            NSCAssert(ShouldShowPanel(@"com.openai.codex", NO), @"Codex and Work must show panel");
            NSCAssert(ShouldShowPanel(@"com.openai.chat", NO), @"ChatGPT must show panel");
            NSCAssert(!ShouldShowPanel(@"com.apple.finder", NO), @"Other apps must hide panel");
            NSCAssert(!ShouldShowPanel(@"com.openai.codex", YES), @"Manual hide must persist");
            NSCAssert(!ShouldShowPanel(nil, NO), @"Unknown foreground must hide panel");
            NSCAssert([AccountUsageText(@{@"limits": @[@{@"limit_id": @"codex", @"used_percent": @29}]}) isEqual:@"29% used"], @"Account usage percentage");
            NSCAssert([AccountUsageText(@{@"stale": @YES, @"limits": @[@{@"limit_id": @"codex", @"used_percent": @29}]}) isEqual:@"29% used (stale)"], @"Old readings must be marked stale");
            NSCAssert([AccountUsageText(nil) isEqual:@"Usage unavailable"], @"Missing usage is not zero");
            [NSApplication sharedApplication];
            App *test = [App new];
            test.panel = [[UsagePanel alloc] initWithContentRect:NSMakeRect(100,100,PanelWidth,154)
                styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
            test.chatNumbers = [NSMutableDictionary new];
            [test render:@[] message:@"Test empty state"];
            CGFloat expandedHeight = test.panel.frame.size.height;
            CGFloat top = NSMaxY(test.panel.frame);
            [test toggleCollapsed:nil];
            NSCAssert(test.panel.frame.size.height == HeaderHeight, @"Minimized panel shows only header");
            NSCAssert(fabs(NSMaxY(test.panel.frame)-top) < 1, @"Minimizing keeps header position");
            for (NSView *child in test.panel.contentView.subviews)
                NSCAssert(![child isKindOfClass:NSScrollView.class], @"Minimized panel hides session rows");
            [test render:@[] message:@"Refreshed empty state"];
            NSCAssert(test.panel.frame.size.height == HeaderHeight, @"Refresh preserves minimized state");
            [test toggleCollapsed:nil];
            NSCAssert(test.panel.frame.size.height == expandedHeight, @"Expanding restores session area");
            puts("Foreground visibility, account usage, and minimize checks passed"); return 0;
        }
        NSApplication *app = NSApplication.sharedApplication;
        App *delegate = [App new]; app.delegate = delegate; [app run];
    }
    return 0;
}
