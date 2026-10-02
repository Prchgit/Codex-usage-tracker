#import "ScreenDetection.h"

// Codex and ChatGPT Work share com.openai.codex in the current desktop build.
static NSArray<NSString *> *SupportedBundles(void) {
    NSString *configured = NSProcessInfo.processInfo.environment[@"CUT_FOREGROUND_BUNDLES"];
    if (configured.length) {
        NSMutableArray *bundles = [NSMutableArray new];
        for (NSString *entry in [configured componentsSeparatedByString:@","]) {
            NSString *bundle = [entry stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
            if (bundle.length) [bundles addObject:bundle];
        }
        if (bundles.count) return bundles;
    }
    return @[@"com.openai.codex", @"com.openai.chat"];
}

BOOL ShouldShowPanel(NSString *bundle, BOOL manuallyHidden) {
    return !manuallyHidden && bundle != nil && [SupportedBundles() containsObject:bundle];
}
