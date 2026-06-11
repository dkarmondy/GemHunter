"""Tunable knowledge for the scorer — the brain, as editable Python.

Mirrors docs/ (watch-playbook, chronograph-movements, taste-and-targets). All matching
is case-insensitive substring against the title (+ item aspects when enriched).
Edit freely to tune what GemHunter values; nothing here is load-bearing structurally.
"""

# ============================ HARD EXCLUSIONS ============================
# Any hit => reject outright (fail the gate).

EXCLUDE_BRANDS = [
    "fossil", "michael kors", " mk ", "invicta", "stuhrling", "diesel", "guess",
    "armani", "daniel wellington", "swatch", "bulova", "timex",
]

EXCLUDE_KEYWORDS = [
    # form factor / not-a-wristwatch
    "pocket watch", "smart watch", "smartwatch", "apple watch", "garmin", "fitbit",
    "fitness", "digital watch",
    # parts-only / components (distinct from a for-repair WATCH, which we want)
    "movement only", "dial only", "case only", "hands only", "parts lot",
    "spares or repair lot", "lot of watch",
    " stem", "winding stem", "balance complete", "balance staff", "mainspring",
    "hairspring", "bezel insert", "bezel only", "crown only", "dial for",
    "caseback only", "movement spacer", "rotor weight",
    "case and bracelet", "case & bracelet", "case and dial",
    # eBay Live listings
    "ebay live",
    # replica / redial tells (keep "tribute" — legit, e.g. IWC Tribute to 3705)
    "replica", " aaa ", "homage", "redial", "re-dial", "refinished dial",
    "aftermarket dial", "repainted dial",
]

QUARTZ_KEYWORDS = ["quartz"]                 # also checked against the Movement aspect
# Known quartz model lines where sellers often omit the movement (your eye catches these
# from the layout; encode the experience). Excluded by name.
QUARTZ_MODELS = ["formula 1", "formula one"]
DISLIKED_CALIBERS = ["7733", "7734", "7736"] # cam, noisy — user dislikes

RUSSIAN_BRANDS = ["poljot", "vostok", "raketa", "slava", "sekonda", "molnija"]
CHINESE_BRANDS = ["seagull", "sea-gull"]
JAPANESE_BRANDS = ["seiko", "citizen", "orient", "casio", "g-shock", "miyota",
                   "pulsar", "lorus"]
JAPANESE_EXCEPTIONS = ["grand seiko", "king seiko", "credor"]  # vintage only, allowed

AGE_FLOOR_YEAR = 1960  # nothing older (too fragile to service)

# ============================ TASTE SIGNALS ============================
# Brands he hunts (Swiss vintage/neo-vintage + wishlist).
TASTE_BRANDS = [
    "patek", "philippe", "rolex", "omega", "iwc", "breitling", "tudor",
    "jaeger", "lecoultre", "jlc", "vacheron", "constantin", "audemars", "piguet",
    "lange", "sohne", "söhne", "zenith", "universal gen", "heuer", "gallet",
    "wakmann", "gigandet", "breguet", "chopard", "blancpain", "longines",
    "movado", "minerva", "angelus", "sinn", "nomos", "grand seiko",
    "excelsior park", "enicar", "doxa", "girard", "perregaux", "eberhard",
    "ulysse nardin", "glashut", "f.p. journe", "journe",
]
CHRONO_KEYWORDS = ["chronograph", "chrono"]

# In-house / haute brands get an extra grade boost (substring -> bonus points).
BRAND_GRADE = {
    "iwc": 2, "patek": 3, "vacheron": 3, "lange": 3, "audemars": 3,
    "breguet": 3, "journe": 3,
}

# Caliber keyword -> (points, is_column_wheel). Column-wheel = premium.
VALUED_CALIBERS = {
    "valjoux 72": (6, True), "valjoux 730": (6, True), "valjoux 92": (6, True),
    "valjoux 88": (7, True), "valjoux 23": (5, True), "valjoux 90": (5, True),
    "venus 175": (6, True), "venus 178": (5, True), "venus 150": (5, True),
    "lemania 2310": (8, True), "caliber 321": (8, True), "cal 321": (8, True),
    "cal. 321": (8, True), "el primero": (7, True), "excelsior park": (6, True),
    "longines 13zn": (8, True), "longines 30ch": (6, True), "minerva": (5, True),
    "valjoux 7750": (2, False), " 7750": (1, False), "lemania 5100": (3, False),
    "caliber 11": (4, False), "cal 11": (4, False), "cal 12": (4, False),
}

# Condition / opportunity signals
PROJECT_KEYWORDS = [          # as-is / Path B buy signal
    "as-is", "as is", "for parts", "not working", "for repair", "untested",
    "running but", "needs service", "non-running", "non running", "not running",
    "doesn't run", "does not run", "spares or repair", "incomplete", "won't run",
    "stopped", "parts or repair",
]
POSITIVE_CONDITION = [        # condition premium
    "box and papers", "box & papers", "full set", "unpolished",
    "new old stock", " nos ", "all original", "original dial",
]
NEGATIVE_KEYWORDS = ["rust", "water damage", "heavily polished"]

# ============================ WEIGHTS ============================
W_BRAND = 3            # brand in taste list
W_CHRONO = 2           # chronograph
W_COLUMN_WHEEL = 2     # extra on top of caliber points if column-wheel
W_PROJECT = 4          # as-is opportunity (Path B)
W_BOX_PAPERS = 3       # box & papers / full set / unpolished
W_AUTH_GUARANTEE = 2   # eBay Authenticity Guarantee (trust on higher-value)
W_SIZE_OK = 3          # case size >= 36mm (aspect)
W_SIZE_SMALL = -3      # case size < 34mm (too tiny / toy)
W_SOLID_SCREW = 1      # solid steel / screw-down caseback (aspect)
W_NEGATIVE = -5        # rust / water damage / heavily polished

TASTE_MIN = 3.0        # taste-gate threshold: below this, drop even if a "deal"
SIZE_MIN = 36.0
SIZE_TINY = 34.0

# ============================ PATH A: COLLECTOR (nice / box & papers) ============================
# The "box & papers" stream — full-set, original, unpolished pieces from great sellers.
# Desired models (beyond TASTE_BRANDS) for this stream, esp. Rolex Sub/Daytona.
COLLECTOR_TARGETS = [
    "submariner", "daytona", "gmt-master", "gmt master", "sea-dweller", "sea dweller",
    "explorer", "datejust", "day-date", "yacht-master", "milgauss", "oyster perpetual",
    "datograph", "lange 1", "nautilus", "aquanaut", "royal oak", "overseas",
    "speedmaster", "moonwatch", "navitimer", "el primero", "reverso", "calatrava",
]
NO_REPAIR_BRANDS = ["rolex"]   # never a repair project — route to Path A only
# Rolex models that get their own "Box & Papers Rolex" tab (others → Other tab).
ROLEX_TARGETS = ["submariner", "gmt-master", "gmt master", "daytona"]

FULLSET_KEYWORDS = ["box and papers", "box & papers", "box & paper", "box and paper",
                    "full set", "complete set", "box papers", "with papers", "b&p"]
ORIGINAL_KEYWORDS = ["unpolished", "un-polished", "all original", "original dial",
                     "new old stock", " nos ", "mint", "near mint", "unworn"]

W_TARGET = 3           # desired brand (reuses W_BRAND value)
W_MODEL = 2            # specific collector model name (Submariner, Daytona, …)
W_FULLSET = 6          # box & papers / full set — the heart of Path A
W_ORIGINAL = 3         # unpolished / all original / mint
W_GREAT_SELLER = 3     # established high-feedback seller
W_POLISHED = -4        # "polished"/refinished (opposite of what we want here)
COLLECTOR_MIN_TASTE = 3.0
GREAT_SELLER_SCORE = 1000
GREAT_SELLER_PCT = 99.0
