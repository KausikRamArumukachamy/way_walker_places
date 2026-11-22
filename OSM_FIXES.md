# Fixing place names at the source (OpenStreetMap)

Waywaker's places come from OpenStreetMap (OSM). A nickname added in OSM
reaches the app at the next monthly build, for every user, and helps every
other map app too. `assets/places/aliases.txt` is the quick fix inside the
app; OSM is the lasting one.

## One-time setup

1. Make a free account at https://www.openstreetmap.org/user/new
2. Edit in the browser: find the place, click **Edit** (the iD editor).

## Adding a nickname

1. Search the place (e.g. "Kempegowda Bus Station") and click it on the map.
2. In the left panel, scroll to **Tags**, switch to text view.
3. Add a line, then **Save** with a short note ("add common local name"):

| Tag | Use for | Example |
|---|---|---|
| `alt_name` | Another name people really use | `alt_name=Majestic Bus Stand` |
| `old_name` | A former official name | `old_name=Victoria Terminus` |
| `short_name` | An abbreviation | `short_name=CMBT` |
| `official_name` | The full government name | `official_name=Kalaignar Centenary Bus Terminus` |

Several values go in one tag, separated by `;` (`alt_name=MGBS;Imlibun`).
Waywaker's builder already reads all four tags.

## Rules OSM cares about

- Only add names people actually use on the ground. No marketing, no
  made-up names, no copying from Google Maps (its data is copyrighted).
- Edit by hand. Automated or bulk edits need approval from the OSM
  community first: https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct
- One place per change is fine; OSM volunteers may review your edits.

## Suggested first edits (checked against the India place file)

| Place in OSM | Add |
|---|---|
| Kempegowda Bus Station (Bengaluru) | `alt_name=Majestic Bus Stand` |
| Koyambedu Mofussil Bus Terminal (Chennai) | `short_name=CMBT` |
| Kilambakkam Moffusil Terminus (Chennai) | `official_name=Kalaignar Centenary Bus Terminus`, `short_name=KCBT` |
| Mahatma Gandhi Bus Station (Hyderabad) | `short_name=MGBS`, `alt_name=Imlibun` |
| Chhatrapati Shivaji Maharaj Terminus (Mumbai) | `old_name=Victoria Terminus`, `short_name=CSMT` |
| Krantivira Sangolli Rayanna (Bengaluru) station | `alt_name=Bangalore City`, `short_name=KSR` |

Look at the existing tags first — if the name is already there under
another tag, leave it.

After editing, the next GitHub monthly build picks it up. Once a name is in
OSM, its line in `aliases.txt` can be removed.
