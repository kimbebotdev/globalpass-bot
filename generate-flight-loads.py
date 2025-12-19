import json
import pandas as pd
from datetime import datetime

def normalize_google_time(time_str):
    """Converts 12h time (Google) to 24h 'HH:MM' for matching."""
    try:
        time_str = time_str.replace('\u202f', ' ').strip()
        return datetime.strptime(time_str, "%I:%M %p").strftime("%H:%M")
    except:
        return None

def to_minutes(duration_str):
    """Converts duration strings like '7h 25m' to total minutes."""
    if not duration_str: return 1440
    try:
        clean = duration_str.lower().replace('hr', 'h').replace('min', 'm').replace(' ', '')
        h = int(clean.split('h')[0]) if 'h' in clean else 0
        m_part = clean.split('h')[-1] if 'h' in clean else clean
        m = int(m_part.replace('m', '')) if 'm' in m_part else 0
        return h * 60 + m
    except: return 1440

def generate_multi_sheet_report():
    # Load all sources
    with open('json/flightschedule.json', 'r') as f:
        myid_data = json.load(f)
    with open('json/google_flights_results.json', 'r') as f:
        google_data = json.load(f)
    with open('json/stafftraveller_results.json', 'r') as f:
        staff_data = json.load(f)

    # Pre-process external sources for matching
    staff_map = {d['airline_flight_number']: d.get('aircraft', 'N/A') 
                 for entry in staff_data for d in entry.get('flight_details', [])}
    
    google_map = {}
    for entry in google_data:
        for ftype in ['top_flights', 'other_flights']:
            for g_f in entry.get('flights', {}).get(ftype, []):
                norm_time = normalize_google_time(g_f.get('depart_time', ''))
                if norm_time: google_map[(g_f['airline'], norm_time)] = True

    chance_weights = {"HIGH": 100, "MID": 50, "LOW": 10}
    eligible_flights = []

    # Filter and score selectable flights
    for routing in myid_data.get('routings', []):
        for flight in routing.get('flights', []):
            if flight.get('selectable') is not True: continue
            
            seg = flight['segments'][0]
            f_num, airline, dep_time = seg['flightNumber'], seg['operatingAirline']['name'], seg['departureTime']
            
            # Source detection
            in_staff = f_num in staff_map
            in_google = (airline, dep_time) in google_map
            sources = ["MyIDTravel.com"]
            if in_google: sources.append("Google Flights")
            if in_staff: sources.append("Stafftraveler")
            
            # Scoring logic
            dur_min = to_minutes(flight.get('duration', '0h 0m'))
            score = chance_weights.get(flight.get('chance', 'LOW'), 0)
            score += 20 if len(flight.get('segments', [])) == 1 else 0 # Nonstop bonus
            score += max(0, (720 - dur_min) / 10) # Duration bonus

            eligible_flights.append({
                "Flight": f_num, "Airline": airline, "Aircraft": staff_map.get(f_num, "N/A"),
                "Departure": dep_time, "Arrival": seg['arrivalTime'], "Duration": flight.get('duration'),
                "Chance": flight.get('chance'), "Source": ", ".join(sources), "Score": round(score, 2),
                "In_Staff": in_staff, "In_Google": in_google
            })

    # Ranking helper function
    def get_top_5(subset):
        ranked = sorted(subset, key=lambda x: x['Score'], reverse=True)
        final = []
        for i, item in enumerate(ranked[:5], 1):
            clean = {"Rank": i}
            clean.update({k: v for k, v in item.items() if not k.startswith("In_")})
            final.append(clean)
        return final

    # Generate output lists
    results = {
        "Top_5_Overall": get_top_5(eligible_flights),
        "Top_5_MyIDTravel": get_top_5(eligible_flights),
        "Top_5_Stafftraveler": get_top_5([f for f in eligible_flights if f['In_Staff']]),
        "Top_5_Google_Flights": get_top_5([f for f in eligible_flights if f['In_Google']])
    }

    # Save to JSON and Excel
    with open('standby_report_multi.json', 'w') as f: json.dump(results, f, indent=4)
    with pd.ExcelWriter('standby_report_multi.xlsx') as writer:
        pd.DataFrame(results["Top_5_Overall"]).to_excel(writer, sheet_name='Top 5 Overall', index=False)
        pd.DataFrame(results["Top_5_MyIDTravel"]).to_excel(writer, sheet_name='MyIDTravel', index=False)
        pd.DataFrame(results["Top_5_Stafftraveler"]).to_excel(writer, sheet_name='Stafftraveler', index=False)
        pd.DataFrame(results["Top_5_Google_Flights"]).to_excel(writer, sheet_name='Google Flights', index=False)

if __name__ == "__main__":
    generate_multi_sheet_report()