# Helpers

Drop one-off scripts and utilities here.

Run from repo root:
- `python helpers/your_script.py`
- `python -m helpers.your_script` (requires \_\_init__.py)

Import account JSONs:
- `python helpers/account_exporter.py import --myidtravel helpers/flight-master-accounts.json --stafftraveler helpers/stafftraveler-accounts.json --truncate`

Export account JSONs:
- `python helpers/account_exporter.py export --type flight-master --file helpers/Sample\ Flight\ Master.xlsx`
- `python helpers/account_exporter.py export --type stafftraveler --file helpers/Sample\ StaffTraveler\ Accounts.xlsx`
