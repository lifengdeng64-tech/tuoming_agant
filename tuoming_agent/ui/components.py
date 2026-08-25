from __future__ import annotations

import html

import streamlit as st


def render_page_intro(eyebrow: str, title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="page-intro">
            <span>{html.escape(eyebrow)}</span>
            <h2>{html.escape(title)}</h2>
            <p>{html.escape(subtitle)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_kpi_card(label: str, value: str, note: str) -> None:
    st.markdown(
        f"""
        <div class="insight-card">
            <span>{html.escape(label)}</span>
            <strong>{html.escape(value)}</strong>
            <small>{html.escape(note)}</small>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(label: str, symbol: str = "＋") -> None:
    st.markdown(
        f"""
        <div class="empty-state">
            <strong>{html.escape(symbol)}</strong>
            <span>{html.escape(label)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

