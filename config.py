"""
Centralized configuration for selectors, URLs, and output paths.
"""
from pathlib import Path

# Login
LOGIN_URL = "https://signon.ual.com/oamfed/idp/initiatesso?providerid=DPmyidtravel"

# Possible home URLs (try in order)
BASE_URLS = [
    "https://myidtravel-united.ual.com/myidtravel/",
    "https://www.myidtravel.com/myidtravel/",
    "https://swa.myidtravel.com/myidtravel/",
    "https://myidtravel.com/myidtravel/",
]

# Form selectors
ORIGIN_SELECTOR = "#Origin"
DEST_SELECTOR = "#Destination"
DATE_SELECTOR = "#date-picker"
AIRLINE_SELECTOR = "#input-airline"
TRAVEL_STATUS_SELECTOR = "#input-travelstatus"
TIME_SELECTOR = "#Time"
CLASS_SELECTOR = "#Class"
NEW_FLIGHT_SELECTOR = "#new-flight"
SUBMIT_SELECTOR = ".styles_searchButton__m0ovc"
FLIGHT_TYPE = ".styles_findFlightsTabsContainer__gw3cO"

# Output
FLIGHTSCHEDULE_OUTPUT = Path("json/flightschedule.json")
