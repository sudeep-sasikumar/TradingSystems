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
from tabs.tab_52wh_historic import render_tab as render_52wh_historic
from tabs.tab_regime import render_tab as render_regime
from tabs.tab_sp500 import render_tab as render_sp500
from tabs.tab_setup import render_tab as render_setup

st.set_page_config(
    page_title="TradingSystems Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("TradingSystems Dashboard")
st.caption("52-Week High Momentum Strategy | NSE Nifty 500 + S&P 500")

tabs = st.tabs([
    "Nifty 500 — Live",
    "Nifty 500 — Historic",
    "Nifty 500 — Regime Analysis",
    "S&P 500",
    "Setup & Admin",
])

with tabs[0]:
    render_52wh()

with tabs[1]:
    render_52wh_historic()

with tabs[2]:
    render_regime()

with tabs[3]:
    render_sp500()

with tabs[4]:
    render_setup()
