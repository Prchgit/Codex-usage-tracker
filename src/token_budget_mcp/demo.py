import json
from .core import BudgetService, DemoProvider

def main():
    service = BudgetService(':memory:', DemoProvider())
    preview = service.preview([{'role':'user','content':'Extract the city from: I live in Singapore.'}],task_type='extraction')
    report = service.execute(preview['preview_id'],preview['recommendation']['model'],True)
    print(json.dumps({'preview':preview,'report':report},indent=2))
    service.close()

if __name__ == '__main__':
    main()
