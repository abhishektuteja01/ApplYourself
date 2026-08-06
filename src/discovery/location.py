import re
import unicodedata
from dataclasses import dataclass
import geonamescache


def _fold(s: str) -> str:
    """Lowercase and strip diacritics so scraped ASCII forms match the
    accented geonames spellings (e.g. "Iasi" -> geonames "Iaşi", "Zurich"
    -> "Zürich", "Sao Paulo" -> "São Paulo")."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower()

@dataclass
class LocationParse:
    country: str
    state: str
    city: str
    remote: bool

gc = geonamescache.GeonamesCache()
US_STATES = gc.get_us_states()
COUNTRIES = gc.get_countries()
CITIES = gc.get_cities()

# Build lookups
STATE_CODES = {v['code']: v['name'] for v in US_STATES.values()}
STATE_NAMES = {v['name'].lower(): v['code'] for v in US_STATES.values()}

COUNTRY_NAMES = {v['name'].lower(): v['name'] for v in COUNTRIES.values()}
COUNTRY_NAMES.update({
    'usa': 'United States',
    'us': 'United States',
    'united states': 'United States',
    'united states of america': 'United States',
    'uk': 'United Kingdom',
})

US_CITIES = {}
# Sort by population descending so we get the biggest city for a name
for city in sorted(CITIES.values(), key=lambda x: x.get('population', 0), reverse=True):
    if city['countrycode'] == 'US':
        name = city['name'].lower()
        if name not in US_CITIES:
            US_CITIES[name] = city

# ISO2 country code -> canonical country name, for turning a foreign city's
# `countrycode` into the same country string the country-name matcher emits.
CC_TO_COUNTRY = {code: data['name'] for code, data in COUNTRIES.items()}

# Diacritic-folded set of every US city name, so a foreign city that is ALSO
# a US city (Paris/Manchester/Cambridge/Vancouver/Portland ...) is left to the
# US matcher and never counts as a foreign signal — we stay conservative and
# read a bare namesake as its US city.
US_CITY_FOLDED = {_fold(name) for name in US_CITIES}

# Major non-US cities, keyed by diacritic-folded lowercase name -> country.
# Population floor keeps this to real world cities (and off short place-names
# that collide with ordinary tokens); namesake collisions with US cities are
# excluded above. Sorted by population so the biggest bearer of a name wins
# (e.g. "london" -> London, GB, not London, ON).
FOREIGN_CITY_MIN_POP = 150000
FOREIGN_CITIES: dict[str, str] = {}
for city in sorted(CITIES.values(), key=lambda x: x.get('population', 0), reverse=True):
    if city['countrycode'] == 'US':
        continue
    if city.get('population', 0) < FOREIGN_CITY_MIN_POP:
        continue
    folded = _fold(city['name'])
    if folded in US_CITY_FOLDED or folded in FOREIGN_CITIES:
        continue
    country = CC_TO_COUNTRY.get(city['countrycode'], "")
    if country:
        FOREIGN_CITIES[folded] = country

def parse_location(raw: str) -> LocationParse:
    if not raw:
        return LocationParse("", "", "", False)
        
    remote = "remote" in raw.lower()

    # Collect every signal in the string first, then reconcile. A multi-region
    # string like "New York, London or Singapore" must not resolve to NY, so
    # separators are never used to pick one part — the reconcile step below
    # bails out on conflicting signals instead.
    found_countries = set()
    found_states = set()
    found_city_names = set()
    
    text = raw
    text_lower = text.lower()
    
    # 1. Countries, on word boundaries. Names are stripped: an upstream entry
    # with a trailing space makes the closing \b unsatisfiable, so the name
    # matches nothing.
    for c_name, c_canon in COUNTRY_NAMES.items():
        if re.search(r'\b' + re.escape(c_name.strip()) + r'\b', text_lower):
            found_countries.add(c_canon)

    # 2. States, full names first.
    for s_name, s_code in STATE_NAMES.items():
        if re.search(r'\b' + re.escape(s_name.strip()) + r'\b', text_lower):
            found_states.add(s_code)
            
    # two-letter abbreviations ONLY when preceded by a comma+space token boundary ("Austin, TX")
    state_code_matches = re.findall(r',\s+([A-Z]{2})\b', text)
    for code in state_code_matches:
        if code in STATE_CODES:
            found_states.add(code)
            
    # 3. Cities, matched over 1-3 word n-grams rather than substrings, since city
    # names are often ordinary tokens. Unicode letters (not just a-z) so accented
    # spellings like "Zürich"/"São Paulo" tokenize as one word and fold to ASCII.
    words = re.findall(r'[^\W\d_]+', text, re.UNICODE)
    ngrams = []
    for i in range(len(words)):
        ngrams.append(words[i].lower())
        if i < len(words) - 1:
            ngrams.append(f"{words[i].lower()} {words[i+1].lower()}")
        if i < len(words) - 2:
            ngrams.append(f"{words[i].lower()} {words[i+1].lower()} {words[i+2].lower()}")
            
    for ngram in ngrams:
        if ngram in US_CITIES:
            # An n-gram hit anywhere counts, so "San Francisco Bay Area" resolves
            # to San Francisco.
            found_city_names.add(ngram)
        foreign_country = FOREIGN_CITIES.get(_fold(ngram))
        if foreign_country:
            found_countries.add(foreign_country)

    # Reconcile. A US state or city implies the country.
    if found_states or found_city_names:
        found_countries.add("United States")

    # Multiple countries -> a multi-region listing. If the United States is
    # one of them it's a US-inclusive global role: don't guess a single spot,
    # return empty (the caller keeps it). If every country is foreign, the row
    # is positively non-US -> return a foreign country so the allowlist drops
    # it (rather than the old behavior of returning empty and keeping it).
    if len(found_countries) > 1:
        if "United States" in found_countries:
            return LocationParse("", "", "", remote)
        return LocationParse(sorted(found_countries)[0], "", "", remote)

    country = list(found_countries)[0] if found_countries else ""
    state = ""
    city = ""
    
    # Exactly one state is a usable signal; several means a multi-site listing,
    # which resolves to nothing (the caller keeps it).
    if len(found_states) == 1:
        state = list(found_states)[0]
        country = "United States"
    elif len(found_states) > 1:
        return LocationParse("", "", "", remote)

    # A known state disambiguates city namesakes; drop cities that contradict it.
    valid_cities = []
    for c in found_city_names:
        city_data = US_CITIES[c]
        if state and city_data['admin1code'] != state:
            continue
        valid_cities.append(c)

    # A lone city hit fills state + country from geonames when the string didn't
    # provide them ("San Francisco Bay Area" -> San Francisco, CA, US).
    if not state and len(valid_cities) == 1:
        city_data = US_CITIES[valid_cities[0]]
        state = city_data['admin1code']
        country = "United States"
        city = city_data['name']
    elif len(valid_cities) == 1:
        city_data = US_CITIES[valid_cities[0]]
        city = city_data['name']
    return LocationParse(country, state, city, remote)
