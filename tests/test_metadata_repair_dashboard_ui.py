from streamlit.testing.v1 import AppTest


def test_unclassified_is_default_selected_and_can_be_excluded() -> None:
    app = AppTest.from_string(
        """
import streamlit as st
from ui.dashboard import (
    UNCLASSIFIED_FILTER_VALUE,
    metadata_filter_matches,
    render_filter_bar,
)
filters = render_filter_bar(
    {"STORE": "스토어"},
    ["GENERAL_INQUIRY", UNCLASSIFIED_FILTER_VALUE],
    ["NORMAL", UNCLASSIFIED_FILTER_VALUE],
)
st.write("QUEUE_VALUES", filters["queues"])
st.write("PRIORITY_VALUES", filters["priorities"])
st.write(
    "NONE_VISIBLE",
    metadata_filter_matches(None, filters["queues"])
    and metadata_filter_matches(None, filters["priorities"]),
)
"""
    ).run(timeout=30)

    queues = next(item for item in app.multiselect if item.label == "작업 큐")
    priorities = next(
        item for item in app.multiselect if item.label == "우선순위"
    )
    assert "UNCLASSIFIED" in queues.value
    assert "UNCLASSIFIED" in priorities.value
    assert any("NONE_VISIBLE `True`" in item.value for item in app.markdown)

    queues.set_value(["GENERAL_INQUIRY"])
    priorities.set_value(["NORMAL"])
    app = app.run(timeout=30)

    assert any("NONE_VISIBLE `False`" in item.value for item in app.markdown)
