import tempfile, pathlib
from salvage import db
db.settings.database_path = pathlib.Path(tempfile.mkdtemp())/'c.db'
from salvage.controls import controls, AgentMode
from salvage.pipeline import process_batch
from salvage.simulator.generate import generate_events

ev = generate_events(24, seed=5)

def run(label):
    db.reset_db()
    r = process_batch(ev)
    with db.connect() as c:
        x = c.execute('SELECT COUNT(*) n FROM executions').fetchone()['n']
        d = c.execute('SELECT COUNT(*) n FROM decisions').fetchone()['n']
    print(f'  {label:14s} status={r["agent_status"]:12s} executed={str(r["executed"]):5s} decisions={d:3d} executions={x:3d}', flush=True)

print('kill switch and review-first, verified end to end:', flush=True)
run('autonomous')
controls.set(mode=AgentMode.REVIEW_FIRST); run('review-first')
controls.kill(reason='merchant pulled it'); run('killed')
print(f'\n  state: {controls.get().status} | {controls.get().disabled_reason}', flush=True)
