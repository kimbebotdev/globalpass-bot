# Flight Ranking Engine: Standby vs. Bookable

This system consolidates data from MyIDTravel (Actionability), Stafftraveler (Airline Metadata), and Google Flights (Commercial Pricing) to find the "Optimal" flight. 

The formula shifts priorities depending on your **Travel Status** because your definition of a "good flight" changes based on whether your seat is guaranteed.

## 1. R2 Standby Status (The "Risk-Mitigation" Formula)
**Goal:** Don't get left at the airport.

In Standby mode, the aircraft type or a 10-minute time difference matters less than the statistical likelihood of you actually boarding.

* **Selectability (The Gatekeeper):** If a flight is not "Selectable" in MyIDTravel, it is discarded immediately.
* **The Boarding Chance:** We give heavy weight to the "Chance" field (High/Medium/Low). A "Low" chance flight is treated as a high-risk asset.
* **Stops:** Non-stop flights are prioritized because "1-stop" on standby doubles your risk of being stranded at the connection point.

**Human-Readable Formula:**
Score = (Boarding Likelihood) + (Direct Flight Bonus) + (Time Efficiency)

---

## 2. Bookable Status (The "Value-for-Money" Formula)
**Goal:** Get the best experience and price for a guaranteed seat.

In Bookable mode, the "Chance" field is ignored because your seat is confirmed. We shift our focus to your "Product Experience."

* **Price (Google Flights Data):** We look for the lowest commercial price as a benchmark.
* **Aircraft Type:** We add "Comfort Points" for superior aircraft (e.g., an Emirates A380 vs. a generic narrow-body).
* **Duration:** Since you aren't worried about boarding, we purely look for the fastest way to get there.

**Human-Readable Formula:**
Score = (Price Value) + (Aircraft Comfort) + (Time Efficiency)