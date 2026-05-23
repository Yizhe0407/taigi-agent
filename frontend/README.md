# Frontend

Vue Kiosk UI for frontend-selected Yunlin route planning.

## Run

Start the backend route planning API from the repo root:

```bash
cd backend
uv run uvicorn api:app --reload --port 8000
```

Then start the frontend:

```bash
pnpm install
pnpm dev
```

The first screen is the route planner. It shows the configured Kiosk origin
visually, lets the user select and confirm a destination pin on the map, sends
that coordinate to `POST /api/route-plans`, and draws the selected route option
from the returned `[lng, lat]` geometry.

The Vite dev server proxies relative `/api` calls to `http://127.0.0.1:8000` by
default. Copy `.env.example` only when the API target or production API origin
must change.
