# Waywaker offline places

Waywaker's search runs entirely on the phone. No search service, no API key,
no limits, no cost. The place data comes from OpenStreetMap.

| File | What's in it | Where it lives |
|---|---|---|
| `base.db.gz` | Every city, town, railway/metro station, bus station, airport and ferry terminal in the world | Built into the app (`assets/places/`) |
| `<country>.db.gz` | One country: adds every bus stop, village, hamlet, suburb and neighbourhood | Downloaded inside the app (Profile → Offline places) |
| `manifest.json` | List of country files and their sizes | Downloaded by the app |

Only places people travel to are kept. Shops, houses, roads and buildings
are dropped.

## One-time setup (free)

1. Create a free account at https://github.com.
2. Create a **public** repository (Waywaker uses `way_walker_places`). GitHub Actions is
   free for public repositories.
3. Upload everything in this folder (`waypack.py`, `requirements.txt`,
   `README.md` and the `.github` folder) to that repository.
4. Open the repository's **Actions** tab, choose **Build places**, then
   **Run workflow**. It takes a few hours and then publishes a release.
   After that it runs by itself every month to pick up new stops.
5. The app downloads from this repository's latest release
   (`lib/services/place_packs.dart`, `dataUrl`).

## Before each Play Store release

Copy the newest world file into the app:

```sh
curl -L -o assets/places/base.db.gz \
  https://github.com/KausikRamArumukachamy/way_walker_places/releases/latest/download/base.db.gz
```

## Building on your own computer (optional)

```sh
pip install -r requirements.txt
python waypack.py pack india.osm.pbf --id india --cc IN --title India --out packs
python waypack.py finish packs
```

Country downloads: https://download.geofabrik.de

## Licence

Data © OpenStreetMap contributors, under the
[Open Database License](https://www.openstreetmap.org/copyright).
The app must show "© OpenStreetMap contributors" (it does, on the map and in
Profile → About & licences). The generated files are a derived database, so
they must stay under the ODbL. Publishing them openly on GitHub Releases does
that.
