from datetime import datetime
from typing import Any


def is_valid_date_mmddyyyy(value: str) -> bool:
    try:
        datetime.strptime(value, "%m/%d/%Y")
        return True
    except Exception:
        return False


def validate_and_normalize_input(input_data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    normalized = dict(input_data or {})

    flight_type = (normalized.get("flight_type") or "").strip()
    travel_status = (normalized.get("travel_status") or "").strip()
    if not flight_type:
        errors.append("flight_type is required.")
    if not travel_status:
        errors.append("travel_status is required.")

    normalized["airline"] = normalized.get("airline") or ""
    if "nonstop_flights" not in normalized or normalized.get("nonstop_flights") in ("", None):
        normalized["nonstop_flights"] = False
    if "auto_request_stafftraveler" not in normalized or normalized.get("auto_request_stafftraveler") in (
        "",
        None,
    ):
        normalized["auto_request_stafftraveler"] = False

    trips = normalized.get("trips")
    if not isinstance(trips, list) or not trips:
        errors.append("trips must be a non-empty array.")
        trips = []
    for idx, trip in enumerate(trips):
        if not isinstance(trip, dict):
            errors.append(f"trips[{idx}] must be an object.")
            continue
        if not (trip.get("origin") or "").strip():
            errors.append(f"trips[{idx}].origin is required.")
        if not (trip.get("destination") or "").strip():
            errors.append(f"trips[{idx}].destination is required.")

    itinerary = normalized.get("itinerary")
    if not isinstance(itinerary, list) or not itinerary:
        errors.append("itinerary must be a non-empty array.")
        itinerary = []
    for idx, leg in enumerate(itinerary):
        if not isinstance(leg, dict):
            errors.append(f"itinerary[{idx}] must be an object.")
            continue
        date_val = (leg.get("date") or "").strip()
        time_val = (leg.get("time") or "").strip()
        class_val = (leg.get("class") or "").strip()
        if not date_val:
            errors.append(f"itinerary[{idx}].date is required.")
        elif not is_valid_date_mmddyyyy(date_val):
            errors.append(f"itinerary[{idx}].date must be MM/DD/YYYY.")
        if not time_val:
            errors.append(f"itinerary[{idx}].time is required.")
        if not class_val:
            errors.append(f"itinerary[{idx}].class is required.")

    if flight_type == "one-way":
        if len(trips) < 1:
            errors.append("one-way requires at least 1 trip.")
        if len(itinerary) < 1:
            errors.append("one-way requires at least 1 itinerary entry.")
    elif flight_type == "round-trip":
        if len(trips) < 2:
            errors.append("round-trip requires 2 trips.")
        if len(itinerary) < 2:
            errors.append("round-trip requires 2 itinerary entries.")
    elif flight_type == "multiple-legs":
        if len(trips) < 1:
            errors.append("multiple-legs requires at least 1 trip.")
        if len(itinerary) < 1:
            errors.append("multiple-legs requires at least 1 itinerary entry.")

    travellers = normalized.get("traveller", [])
    if travellers and not isinstance(travellers, list):
        errors.append("traveller must be an array.")
        travellers = []
    for idx, traveller in enumerate(travellers or []):
        if not isinstance(traveller, dict):
            errors.append(f"traveller[{idx}] must be an object.")
            continue
        name_val = (traveller.get("name") or "").strip()
        salutation_val = (traveller.get("salutation") or "").strip().upper()
        checked_val = traveller.get("checked")
        if not name_val:
            errors.append(f"traveller[{idx}].name is required.")
        if salutation_val not in {"MR", "MS"}:
            errors.append(f"traveller[{idx}].salutation must be MR or MS.")
        if not isinstance(checked_val, bool):
            errors.append(f"traveller[{idx}].checked must be a boolean.")
        traveller["salutation"] = salutation_val

    partners = normalized.get("travel_partner", [])
    if partners and not isinstance(partners, list):
        errors.append("travel_partner must be an array.")
        partners = []
    for idx, partner in enumerate(partners or []):
        if not isinstance(partner, dict):
            errors.append(f"travel_partner[{idx}] must be an object.")
            continue
        p_type = (partner.get("type") or "").strip().lower()
        if p_type not in {"adult", "child"}:
            errors.append(f"travel_partner[{idx}].type must be Adult or Child.")
        first_name = (partner.get("first_name") or "").strip()
        last_name = (partner.get("last_name") or "").strip()
        if not first_name:
            errors.append(f"travel_partner[{idx}].first_name is required.")
        if not last_name:
            errors.append(f"travel_partner[{idx}].last_name is required.")
        if "own_seat" not in partner or partner.get("own_seat") is None:
            partner["own_seat"] = True
        if p_type == "adult":
            salutation_val = (partner.get("salutation") or "").strip().upper()
            if salutation_val not in {"MR", "MS"}:
                errors.append(f"travel_partner[{idx}].salutation must be MR or MS.")
            partner["salutation"] = salutation_val
        if p_type == "child":
            dob_val = (partner.get("dob") or "").strip()
            if not dob_val:
                errors.append(f"travel_partner[{idx}].dob is required for Child.")
            elif not is_valid_date_mmddyyyy(dob_val):
                errors.append(f"travel_partner[{idx}].dob must be MM/DD/YYYY.")

    return normalized, errors
