from __future__ import annotations

import os

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel, _render_dps


database = Database()
database.initialize()
inquiry_id = int(os.environ["PHASE85_INQUIRY_ID"])
inquiry = InquiryRepository(database).get(inquiry_id)
if inquiry is None:
    raise LookupError(f"Inquiry not found: {inquiry_id}")

_render_dps(database, inquiry)
_render_answer_panel(database, inquiry)
