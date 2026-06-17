"""
TradingSystems Dashboard — Main Entry Point

Run with:
    streamlit run dashboard/app.py

Tab structure is intentionally defined here even for phases not yet built.
To add a new strategy phase: implement its tab module in dashboard/tabs/,
import the render function here, and add it to the tabs list below.
"""
import sys
from pathlib import Path

# Make 'shared', 'tabs' importable
_ROOT = Path(__file__).resolve().parent.parent
_DASH = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DASH))

import streamlit as st
from tabs.tab_52wh import render_tab as render_52wh

st.set_page_config(
    page_title="TradingSystems Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("TradingSystems Dashboard")
st.caption("Phase 1 — 52-Week High Momentum Strategy | NSE / Nifty 500")

tabs = st.tabs(["52-Week High System"])

with tabs[0]:
    render_52wh()

# Phase 2+: add tab modules here
# from tabs.tab_52wh_indicator import render_tab as render_52whi
# tabs = st.tabs(["52-Week High System", "52WH + Indicator"])
# with tabs[1]:
#     render_52whi()
