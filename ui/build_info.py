from __future__ import annotations

from html import escape

import streamlit as st

from core.build_info import get_git_build_info


def render_build_footer() -> None:
    build = get_git_build_info()
    st.markdown(
        '<div class="dashboard-build-footer">'
        f"{escape(build.display)}"
        "</div>",
        unsafe_allow_html=True,
    )
