"""
TradingSystems Dashboard — Main Entry Point

Run with:
    streamlit run dashboard/app.py

3-tab structure:
  Nifty 500    — survivorship-corrected backtest + live + regime + freshness
  S&P 500      — full backtest + regime + freshness + comparison
  Setup & Admin — data setup, DB status, CLI reference
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DASH = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DASH))

import streamlit as st
from tabs.tab_nifty  import render_tab as render_nifty
from tabs.tab_sp500  import render_tab as render_sp500
from tabs.tab_setup  import render_tab as render_setup

st.set_page_config(
    page_title="TradingSystems Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("TradingSystems Dashboard")
st.caption("52-Week High Momentum Strategy | NSE Nifty 500 + S&P 500")

tabs = st.tabs([
    "Nifty 500",
    "S&P 500",
    "Setup & Admin",
])

with tabs[0]:
    render_nifty()

with tabs[1]:
    render_sp500()

with tabs[2]:
    render_setup()
