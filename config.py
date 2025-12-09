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
NONSTOP_FLIGHTS_CONTAINER = ".styles_toggleButtonsWrapper__UrVTR"
LEG_SELECTOR = "div.styles_dateTimeClassContainer__Fku9u"
TRAVELLER_ITEM_SELECTOR = "div.styles_travelSelection_list_element_withoutCollapse__9CVFu"
TRAVELLER_NAME_SELECTOR = "div.styles_userInfoContainer__07zwE"
TRAVELLER_CHECKBOX_SELECTOR = "input[type='checkbox']"
TRAVELLER_SALUTATION_TOGGLE = "#salutaion-dropdownToggle"
TRAVELLER_CONTINUE_BUTTON = "#continue-button-traveller-selection"

# Output
FLIGHTSCHEDULE_OUTPUT = Path("json/flightschedule.json")
