"""Watch knowledge the National Rarities ranking draws on, as editable Python.

Mirrors docs/ (watch-playbook, chronograph-movements, taste-and-targets). All
matching is case-insensitive substring against the listing title.
"""

# Known quartz model lines where sellers often omit the movement. Excluded by name.
QUARTZ_MODELS = ["formula 1", "formula one"]

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

# As-is / needs-work wording: the project lots he buys to service himself.
PROJECT_KEYWORDS = [
    "as-is", "as is", "for parts", "not working", "for repair", "untested",
    "running but", "needs service", "non-running", "non running", "not running",
    "doesn't run", "does not run", "spares or repair", "incomplete", "won't run",
    "stopped", "parts or repair",
]

# Desired collector models beyond TASTE_BRANDS, esp. Rolex Sub/Daytona.
COLLECTOR_TARGETS = [
    "submariner", "daytona", "gmt-master", "gmt master", "sea-dweller", "sea dweller",
    "explorer", "datejust", "day-date", "yacht-master", "milgauss", "oyster perpetual",
    "datograph", "lange 1", "nautilus", "aquanaut", "royal oak", "overseas",
    "speedmaster", "moonwatch", "navitimer", "el primero", "reverso", "calatrava",
]

# IWC golden era (~1980–2005, pre-Richemont). He owns Mark XV, 3706, PD 3701;
# hunting these refs specifically. Checked only when the brand is IWC.
IWC_TARGETS = ["big pilot", "iw5002", "5002", "iwc 3713", "iw3713", "3713",
               "double chronograph", "doppelchronograph", "doppel", "mark xii",
               "mark 12", "iw3241", "3241", "iw9241", "9241", "utc",
               "iw3251", "3251", "mark xv", "3253", "3706",
               "gst", "aquatimer 3536", "3536", "ingenieur 3521", "porsche design"]
