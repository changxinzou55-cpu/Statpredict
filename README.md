# StatPredict

Player and game predictions for the NBA (main) and Europe's top five football leagues, checked against real results.

- Data: ESPN's public JSON feeds (no key). Stored in SQLite under `~/statpredict-data` (override with `STATPREDICT_DATA_DIR`).
- Run: `pip install -r requirements.txt`, then `python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8100`.
  The first start backfills two earlier NBA seasons (a few minutes); after that it refreshes every 5 minutes.
- Model: `app/nba_model.py` (NBA), `app/playoffs.py` (season and playoff simulation for title odds), `app/soccer.py` (football). Settings live in `app/params.json`;
  re-tune on a past season with `python3 -m scripts.tune`.
- Tests: `python3 -m pytest -q tests`.

## Public hosting

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/changxinzou55-cpu/Statpredict)

Sign in to Render and approve the free Blueprint. Once the service is live, share its `onrender.com` URL; visitors don't need accounts.

`Dockerfile` and `render.yaml` run the app on a free Render web service (512 MB; peak use measured at about 300 MB).
On a host with no stored data the app starts from the snapshot in `seed/statpredict.db.gz` and then fetches
everything since from ESPN, so it is usable within seconds. Free hosts wipe the disk on restart: locked
predictions made after the snapshot are lost unless `STATPREDICT_DATA_DIR` points at a persistent disk.
Refresh the snapshot with `python3 -m scripts.make_seed`.
