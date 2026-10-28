"""临时巡检脚本：直接读库看任务与分页进度。"""
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "data/app.db"
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print("=== 最近任务 ===")
for r in c.execute(
    "select id, filename, status, pages_total, page_range, chars,"
    " order_check_pass_rate, created_at from tasks order by created_at desc limit 8"
):
    d = dict(r)
    print(
        f"{str(d['created_at'])[:19]}  {str(d['filename'])[:26]:26s} {d['status']:10s} "
        f"页={d['pages_total']:<5} 区间={str(d['page_range'])[:10]:10s} "
        f"字={d['chars'] or 0:<7} 序={d['order_check_pass_rate']}"
    )

print()
print("=== 分页状态统计 ===")
for r in c.execute(
    "select t.filename, t.status, p.status as ps, count(*) n "
    "from task_pages p join tasks t on t.id = p.task_id "
    "where t.created_at > '2026-10-27' group by t.id, p.status "
    "order by t.created_at desc"
):
    d = dict(r)
    print(f"{str(d['filename'])[:26]:26s} task={d['status']:10s} page={d['ps']:9s} {d['n']}")
