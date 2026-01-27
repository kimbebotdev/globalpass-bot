from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import delete
from sqlmodel import Session, create_engine

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from models import MyidtravelAccount, StafftravelerAccount


class AccountExporter:
    def __init__(self, output_dir: str = "helpers") -> None:
        self.output_dir = output_dir
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    def clean_and_split(self, value: Any) -> list[str]:
        if pd.isna(value) or str(value).strip().lower() == "nan" or str(value).strip() == "":
            return []
        return [item.strip() for item in str(value).split("\n") if item.strip()]

    def map_travellers(self, names_str: Any, dobs_str: Any, relationship_type: str) -> list[dict[str, Any]]:
        names = self.clean_and_split(names_str)
        dobs = self.clean_and_split(dobs_str)
        result: list[dict[str, Any]] = []
        for idx in range(max(len(names), len(dobs))):
            name = names[idx] if idx < len(names) else None
            dob = dobs[idx] if idx < len(dobs) else None
            if name or dob:
                result.append(
                    {
                        "name": name,
                        "birthday": dob,
                        "relationship": relationship_type,
                    }
                )
        return result

    def export_flight_master(self, input_file: str) -> Path:
        df = pd.read_excel(input_file, skiprows=1)
        records: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            travellers: list[dict[str, Any]] = []
            travellers.extend(self.map_travellers(row.iloc[21], row.iloc[22], "Parent 1"))
            travellers.extend(self.map_travellers(row.iloc[25], row.iloc[26], "Parent 2"))
            travellers.extend(self.map_travellers(row.iloc[33], row.iloc[34], "Primary Friend"))
            travellers.extend(self.map_travellers(row.iloc[36], row.iloc[37], "2nd Enrolled Friend"))
            travellers.extend(self.map_travellers(row.iloc[42], row.iloc[43], "Extended Family Buddy"))
            travellers.extend(self.map_travellers(row.iloc[45], row.iloc[46], "Children"))

            records.append(
                {
                    "employee": str(row.iloc[0]).strip(),
                    "username": str(row.iloc[2]).strip(),
                    "password": str(row.iloc[3]).strip(),
                    "gender": str(row.iloc[6]).strip(),
                    "airport": str(row.iloc[7]).strip(),
                    "position": str(row.iloc[8]).strip(),
                    "travellers": travellers,
                }
            )

        return self._save(records, "flight-master-accounts.json")

    def export_staff_traveler(self, input_file: str) -> Path:
        df = pd.read_excel(input_file)
        records: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            records.append(
                {
                    "employee_name": str(row.iloc[0]).strip(),
                    "username": str(row.iloc[1]).strip(),
                    "email": str(row.iloc[2]).strip(),
                    "password": str(row.iloc[3]).strip(),
                }
            )

        return self._save(records, "stafftraveler-accounts.json")

    def _save(self, data: list[dict[str, Any]], filename: str) -> Path:
        path = Path(self.output_dir) / filename
        path.write_text(json.dumps(data, indent=2))
        print(f"Exported {len(data)} records to {path}")
        return path


def _load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return data


def _build_engine() -> Any:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True, interpolate=False)
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/globalpass.db")
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def _import_myidtravel(records: list[dict[str, Any]], session: Session, truncate: bool) -> int:
    if truncate:
        session.exec(delete(MyidtravelAccount))
        session.commit()

    inserted = 0
    for record in records:
        employee_name = record.get("employee_name") or record.get("employee") or ""
        username = record.get("username") or ""
        password = record.get("password") or ""
        if not employee_name or not username or not password:
            continue

        account = MyidtravelAccount(
            employee_name=employee_name,
            username=username,
            password=password,
            gender=record.get("gender"),
            airport=record.get("airport"),
            position=record.get("position"),
            travellers=record.get("travellers") or record.get("travelers"),
        )
        session.add(account)
        inserted += 1

    session.commit()
    return inserted


def _import_stafftraveler(records: list[dict[str, Any]], session: Session, truncate: bool) -> int:
    if truncate:
        session.exec(delete(StafftravelerAccount))
        session.commit()

    inserted = 0
    for record in records:
        employee_name = record.get("employee_name") or record.get("employee") or ""
        username = record.get("username") or ""
        password = record.get("password") or ""
        if not employee_name or not username or not password:
            continue

        account = StafftravelerAccount(
            employee_name=employee_name,
            username=username,
            email=record.get("email"),
            password=password,
        )
        session.add(account)
        inserted += 1

    session.commit()
    return inserted


def _run_export(args: argparse.Namespace) -> None:
    exporter = AccountExporter()
    if args.type == "flight-master":
        exporter.export_flight_master(args.file)
    else:
        exporter.export_staff_traveler(args.file)


def _run_import(args: argparse.Namespace) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        if args.myidtravel:
            records = _load_json(Path(args.myidtravel))
            inserted = _import_myidtravel(records, session, args.truncate)
            print(f"Myidtravel: inserted {inserted} row(s).")
        if args.stafftraveler:
            records = _load_json(Path(args.stafftraveler))
            inserted = _import_stafftraveler(records, session, args.truncate)
            print(f"Stafftraveler: inserted {inserted} row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or import account data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export accounts to JSON.")
    export_parser.add_argument("--type", choices=["flight-master", "stafftraveler"], required=True)
    export_parser.add_argument("--file", required=True, help="Path to the input Excel file.")
    export_parser.set_defaults(func=_run_export)

    import_parser = subparsers.add_parser("import", help="Import accounts from JSON.")
    import_parser.add_argument("--myidtravel", help="Path to myidtravel accounts JSON.")
    import_parser.add_argument("--stafftraveler", help="Path to stafftraveler accounts JSON.")
    import_parser.add_argument("--truncate", action="store_true", help="Delete existing rows before import.")
    import_parser.set_defaults(func=_run_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
