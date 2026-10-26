"""Load the India PIN code list into the pincodes table.

This script runs OUTSIDE the web app: run it by hand whenever you have a new download.
It is not a Flask CLI command and has no API.

    python scripts/load_pincodes.py data/pincodes.csv

Run it AFTER scripts/load_universities.py: states and districts come from the AISHE
files, and each PIN code is linked to the state and district with the same name
(ignoring case, spaces and punctuation). A PIN whose state or district is not found
is still saved, with that link left empty.

Column names are matched using PINCODE_COLUMN_MAP in config.py. Re-running is safe:
a PIN + locality pair already present is skipped. Each run writes one
master_import_batches row (visible through LIST_IMPORT_BATCHES / GET_IMPORT_BATCH).
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import current_app  # noqa: E402

from app.core.spreadsheets import cell, read_rows  # noqa: E402
from app.core.utils import utcnow, valid_pincode  # noqa: E402
from app.enums import ImportDataset, ImportRowOutcome  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import College, District, MasterImportBatch, Pincode, State, University  # noqa: E402


def _norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


class _Upload:
    """Minimal stand-in for an uploaded file, for read_rows()."""

    def __init__(self, filename, data):
        self.filename, self._data = filename, data

    def read(self):
        return self._data


def _exception(batch, where, pin, outcome, reason):
    batch.exceptions = list(batch.exceptions) + [{"row": where, "pincode": pin, "outcome": outcome.value,
                                                  "reason": reason}]


def import_pincodes(filename, data):
    rows = read_rows(_Upload(filename, data))
    cmap = current_app.config["PINCODE_COLUMN_MAP"]
    keys = {}

    def get(row, field):
        if not keys:
            for h in row:
                keys[_norm(h)] = h
        for alias in cmap[field]:
            h = keys.get(_norm(alias))
            if h is not None and row.get(h) not in (None, ""):
                return cell(row[h])
        return None

    states = {_norm(s.name): s.id for s in State.query.all()}
    districts = {(d.state_id, _norm(d.name)): d.id for d in District.query.all()}
    batch = MasterImportBatch(dataset=ImportDataset.PINCODES, filename=os.path.basename(filename),
                              total_rows=len(rows), exceptions=[])
    db.session.add(batch)
    db.session.flush()
    existing = {(p.pincode, _norm(p.locality)) for p in Pincode.query.all()}
    for n, row in enumerate(rows, start=2):
        pin = (get(row, "pincode") or "").split(".")[0]
        if not valid_pincode(pin):
            batch.invalid_rows += 1
            _exception(batch, n, pin, ImportRowOutcome.INVALID, "Invalid PIN code")
            continue
        locality = get(row, "locality")
        key = (pin, _norm(locality))
        if key in existing:
            batch.duplicate_rows += 1
            continue
        state_id = states.get(_norm(get(row, "state")))
        district_id = districts.get((state_id, _norm(get(row, "district")))) if state_id else None
        if state_id is None or district_id is None:
            batch.unmatched_rows += 1
        db.session.add(Pincode(pincode=pin, locality=locality, state_id=state_id, district_id=district_id))
        existing.add(key)
        batch.imported_rows += 1
        if n % 5000 == 0:
            db.session.flush()
    # Re-check universities / colleges whose PIN was unknown before this import.
    pins = {p for p, _ in existing}
    for model in (University, College):
        for inst in model.query.filter(model.pincode_verified.is_(False)).all():
            if inst.pincode in pins:
                inst.pincode_verified = True
    batch.finished_at = utcnow()
    db.session.commit()
    return batch


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load the India PIN code list into the database.")
    parser.add_argument("file", help="India PIN code list (.csv/.xlsx)")
    parser.add_argument("--env", default=os.environ.get("APP_ENV", "development"),
                        help="Config to use: development, testing or production (default: APP_ENV)")
    args = parser.parse_args(argv)

    from app import create_app
    app = create_app(args.env, start_scheduler=False)
    with app.app_context():
        with open(args.file, "rb") as fh:
            batch = import_pincodes(args.file, fh.read())
        print(f"PIN codes: {batch.imported_rows} new, {batch.duplicate_rows} already present, "
              f"{batch.invalid_rows} invalid, {batch.unmatched_rows} without a matching state/district")


if __name__ == "__main__":
    main()
