from __future__ import annotations

import argparse
import json

from repositories.database import Database
from services.inquiry_metadata_repair_service import (
    InquiryMetadataRepairService,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair missing queue/priority/analysis without API calls."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist repairs. Without this flag the command is a dry-run.",
    )
    args = parser.parse_args()
    database = Database()
    database.initialize()
    result = InquiryMetadataRepairService(database).run(
        dry_run=not args.apply
    )
    print(
        json.dumps(
            result.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
