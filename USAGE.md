# Globalpass Bot Usage Guide (Frontend)

Welcome! This guide is for using the Globalpass Bot web interface only. It covers how to run searches, what each field means, and what to expect in the results.

## Two Ways to Use the App

You can use the app in two modes:

1. **Search Flights (Run All Bots)**
   - Use this when you want a full search across MyIDTravel, Google Flights, and StaffTraveler.
   - Generate Top 5 Flight Loads
2. **Search Flight Number**
   - Use this when you already have specific flight numbers and want details + availability fast.

You can switch between them using the button at the top of the page.

## Mode 1: Search Flights (Run All Bots)

### Step-by-step

1. **Select an Employee Account**
   - Required. The rest of the form appears after selecting an account.

2. **Choose Flight Options**
   - **Flight Type**: One-way, Round trip, or Multiple legs / Multi-city.
   - **Travel Status**: R2 Standby or Bookable.
   - **Airline**: Optional filter. Leave blank to search all airlines.
   - **Non-stop only**: Limits results to direct flights when possible.
   - **Auto request flight on StaffTraveler**: If enabled, the bot will auto-post to StaffTraveler when needed.

3. **(Optional) Add Travel Partners**
   - Use this if you’re traveling with adults or children.
   - You can also select saved travellers from the account.
   - Limit: up to 8 saved travellers and up to 2 additional travel partners.

4. **Add Trips & Itinerary**
   - Click **+ Add Flight** to add each leg.
   - For every leg, fill:
     - **Origin** (airport code)
     - **Destination** (airport code)
     - **Date**
     - **Time** (optional)
     - **Class** (Economy, Premium Economy, Business, First)

5. **Run the Search**
   - Click **Run All Bots** and wait for results.

### What you’ll see

- A progress tracker for each bot (MyIDTravel, Google Flights, StaffTraveler).
- Once complete, use the Download Excel button on the right to access the results summary.

## Mode 2: Search Flight Number

This mode is faster when you already know the exact flight number.

### Step-by-step

1. **Select a StaffTraveler Account**
   - Required before the rest of the form appears.

2. **Choose Flight Options**
   - **Flight Type**: One-way, Round trip, or Multiple legs / Multi-city.
   - **Non-stop only**: Filters to non-stop results when possible.
   - **Airline**: Optional filter.
   - **Auto request on StaffTraveler**: Auto-posts to StaffTraveler if results are missing.

3. **Add Legs**
   - Each leg requires:
     - **Flight number**
     - **Origin**
     - **Destination**
     - **Date**
     - **Time**

4. **Choose Class**
   - **Economy Only**
   - **Business Only**
   - **Both**

5. **Search Flight**
   - Click **Search Flight** and wait for results.

### What you’ll see

- A progress tracker for Google Flights and StaffTraveler.
- A results card per leg showing:
  - Google Flights details
  - StaffTraveler availability
- A **Download Excel** button once the run completes.

### How seat class results are shown (Google Flights)

- The Google Flights card always shows **Economy** and **Business**.
- If you select **Economy Only**, the Economy number shows and Business is `-`.
- If you select **Business Only**, the Business number shows and Economy is `-`.
- If you select **Both**, both will show when available (otherwise `-`).

## Inputs You Should Double-Check

- Airport codes are correct (e.g., `DXB`, `LHR`, `JFK`).
- Dates are correct and in the future.
- Flight numbers are accurate (no spaces if possible).
- For multi-leg trips, make sure each leg has its own date and time.

## What to Expect During a Run

- The status badge updates as the bots work.
- If anything is missing, you’ll see inline error messages.
- If a bot fails, the run can still finish with partial results.

## Downloads

- **Run All Bots**: Download from the right panel when complete.
- **Search Flight Number**: Download from the “Seat Availability” panel when complete.

If the download button is disabled, the run is still processing or failed.
