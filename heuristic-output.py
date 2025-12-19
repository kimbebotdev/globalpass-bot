import json
import csv
import pandas as pd

def get_heuristic_options():
    # Load data sources
    with open('json/flightschedule.json', 'r') as f:
        myid = json.load(f)
    with open('json/stafftraveller_results.json', 'r') as f:
        staff = json.load(f)

    # Map Aircraft for capacity logic (A380 > B773 > Others)
    staff_map = {}
    for entry in staff:
        for detail in entry.get('flight_details', []):
            staff_map[detail['airline_flight_number']] = detail.get('aircraft', 'N/A')

    # Chance mapping for sorting
    chance_rank = {"HIGH": 0, "MID": 1, "LOW": 2}
    
    # Capacity ranking (Higher seats = better standby odds)
    capacity_rank = {"A380": 0, "B773": 1, "B77W": 1, "N/A": 2}

    processed = []

    for routing in myid.get('routings', []):
        for flight in routing.get('flights', []):
            # Strict Filter: selectable only
            if flight.get('selectable') is not True:
                continue
            
            seg = flight['segments'][0]
            f_num = seg['flightNumber']
            aircraft = staff_map.get(f_num, "N/A")

            processed.append({
                "flight_number": f_num,
                "airline": seg['operatingAirline']['name'],
                "aircraft": aircraft,
                "departure": seg['departureTime'],
                "chance": flight.get('chance', 'LOW'),
                # Sort Keys: Chance (lower is better), Time (earlier better), Capacity (higher better)
                "chance_sort": chance_rank.get(flight.get('chance'), 2),
                "time_sort": seg['departureTime'],
                "cap_sort": capacity_rank.get(aircraft, 2)
            })

    # Sort logic: 1. Chance Priority -> 2. Departure Time -> 3. Aircraft Capacity
    ranked_flights = sorted(processed, key=lambda x: (x['chance_sort'], x['time_sort'], x['cap_sort']))

    # Cleanup for output
    final_output = []
    for f in ranked_flights[:5]:
        final_output.append({
            "flight_number": f['flight_number'],
            "airline": f['airline'],
            "aircraft": f['aircraft'],
            "departure": f['departure'],
            "chance": f['chance'],
            "strategy": "First-of-day/Capacity Tiering"
        })

    # Save outputs
    with open('json/heuristic_standby_results.json', 'w') as f:
        json.dump(final_output, f, indent=4)
    
    # pd.DataFrame(final_output).to_csv('heuristic_standby_results.csv', index=False)
    
    return final_output

if __name__ == "__main__":
    results = get_heuristic_options()
    print(pd.DataFrame(results).to_string(index=False))