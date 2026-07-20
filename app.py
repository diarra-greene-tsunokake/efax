import os
import re
import imaplib
import email
import traceback
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

import fitz
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Inbound Fax Dashboard", layout="wide")

DATA_DIR = Path("./received_faxes")
DATA_DIR.mkdir(exist_ok=True)
CLIENT_FILE = Path("clients.json")
DEFAULT_CLIENTS = {
    "81363651163": "ASNET",
    "81363651164": "Example Client",
}
PAGE_SIZE = 8


def normalize_number(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def load_client_directory() -> Dict[str, str]:
    if CLIENT_FILE.exists():
        try:
            with CLIENT_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {normalize_number(k): v for k, v in data.items() if k}
        except Exception:
            pass
    return DEFAULT_CLIENTS.copy()


def save_client_directory(directory: Dict[str, str]) -> None:
    try:
        with CLIENT_FILE.open("w", encoding="utf-8") as f:
            json.dump(directory, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def lookup_client(caller_id: str) -> str:
    directory = load_client_directory()
    normalized = normalize_number(caller_id)
    if not normalized:
        return "Unknown Sender"
    return directory.get(normalized, "Unknown Sender")


def extract_caller_id(message: email.message.Message) -> str:
    headers_to_try = [
        "X-J-2-Caller-ID",
        "X J 2 Caller ID",
        "X-J2-Caller-ID",
        "X-Caller-ID",
        "Caller-ID",
    ]
    for header in headers_to_try:
        value = message.get(header)
        if value:
            return value.strip()
    subject = message.get("Subject", "")
    if subject:
        match = re.search(r"(\d{8,15})", subject)
        if match:
            return match.group(1)
    return ""


def safe_filename(base: str, fallback: str = "fax") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return cleaned or fallback


def save_pdf_attachment(message: email.message.Message, caller_id: str, received_at: datetime) -> Path:
    for part in message.walk():
        content_type = part.get_content_type()
        filename = part.get_filename()
        if content_type != "application/pdf" and not (filename and filename.lower().endswith(".pdf")):
            continue
        if not filename:
            filename = f"fax_{received_at.strftime('%Y%m%d_%H%M%S')}.pdf"
        safe_name = safe_filename(Path(filename).stem or "fax")
        target_path = DATA_DIR / f"{received_at.strftime('%Y%m%d_%H%M%S')}_{safe_name}_{caller_id or 'unknown'}.pdf"
        if target_path.exists():
            return target_path
        payload = part.get_payload(decode=True)
        if payload:
            target_path.write_bytes(payload)
            return target_path
    raise FileNotFoundError("No PDF attachment found")


def create_thumbnail(pdf_path: Path, output_path: Path) -> Path:
    if output_path.exists():
        return output_path
    try:
        doc = fitz.open(pdf_path)
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(output_path)
        doc.close()
        return output_path
    except Exception:
        return output_path


def fetch_inbound_faxes(email_address: str, app_password: str) -> Tuple[List[Dict[str, object]], List[str]]:
    logs: List[str] = []
    faxes: List[Dict[str, object]] = []

    if not email_address or not app_password:
        logs.append("Missing Gmail address or app password.")
        return faxes, logs

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_address, app_password)
        logs.append("Connected to Gmail IMAP successfully.")
        mail.select("INBOX")
        status, message_ids = mail.search(None, "FROM", "message@inbound.efax.com")
        if status != "OK":
            logs.append(f"SEARCH failed with status: {status}")
            mail.logout()
            return faxes, logs
        ids = [item for item in message_ids[0].split() if item]
        logs.append(f"Found {len(ids)} inbound eFax message(s).")

        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    logs.append(f"Fetch failed for message {msg_id}: {status}")
                    continue
                raw_message = msg_data[0][1]
                message = email.message_from_bytes(raw_message)
                received_at = datetime.now()
                date_header = message.get("Date")
                if date_header:
                    try:
                        from email.utils import parsedate_to_datetime
                        parsed = parsedate_to_datetime(date_header)
                        if parsed:
                            received_at = parsed
                    except Exception:
                        pass
                caller_id = extract_caller_id(message)
                company_name = lookup_client(caller_id)
                pdf_path = save_pdf_attachment(message, caller_id, received_at)
                thumb_path = DATA_DIR / f"{pdf_path.stem}_thumb.png"
                create_thumbnail(pdf_path, thumb_path)
                faxes.append(
                    {
                        "received_at": received_at,
                        "caller_id": caller_id or "Unknown",
                        "company_name": company_name,
                        "pdf_path": pdf_path,
                        "thumb_path": thumb_path,
                    }
                )
            except Exception as exc:
                logs.append(f"Error processing message {msg_id}: {exc}")
        mail.logout()
        logs.append("Disconnected from Gmail IMAP.")
    except Exception as exc:
        logs.append(f"Connection error: {exc}")
        logs.append(traceback.format_exc())
    return faxes, logs


def get_credentials() -> Tuple[str, str]:
    load_dotenv()
    address = os.getenv("GMAIL_ADDRESS") or os.getenv("EMAIL_ADDRESS") or ""
    password = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("APP_PASSWORD") or ""
    try:
        if "gmail" in st.secrets:
            if "address" in st.secrets["gmail"]:
                address = str(st.secrets["gmail"]["address"])
            if "app_password" in st.secrets["gmail"]:
                password = str(st.secrets["gmail"]["app_password"])
    except Exception:
        pass
    return address, password


def init_state() -> None:
    if "current_page" not in st.session_state:
        st.session_state.current_page = 1


def main() -> None:
    init_state()
    st.title("Inbound Fax Dashboard")
    st.caption("A lightweight local dashboard for received eFax documents")

    with st.sidebar:
        st.header("Gmail Connection")
        email_address, app_password = get_credentials()
        email_address = st.text_input("Gmail address", value=email_address)
        app_password = st.text_input("Google App Password", value=app_password, type="password")
        refresh = st.button("Refresh Inbox")
        show_debug = st.checkbox("Show Debug Logs", value=False)
        st.markdown("Store credentials in `.env` or `.streamlit/secrets.toml` for convenience.")

        st.markdown("---")
        st.subheader("Contacts")
        contact_number = st.text_input("Contact fax number", value="", help="Enter digits only")
        contact_name = st.text_input("Contact name", value="", help="Example: ASNET")
        if st.button("Save contact"):
            normalized = normalize_number(contact_number)
            if normalized and contact_name.strip():
                directory = load_client_directory()
                directory[normalized] = contact_name.strip()
                save_client_directory(directory)
                st.success(f"Saved contact: {contact_name.strip()} ({normalized})")
            else:
                st.error("Provide a valid fax number and contact name.")

        directory = load_client_directory()
        if directory:
            with st.expander("Saved contacts", expanded=False):
                for number, name in sorted(directory.items(), key=lambda item: item[0]):
                    st.write(f"{name} — {number}")

    if refresh:
        st.session_state.current_page = 1

    if not email_address or not app_password:
        st.warning("Add your Gmail address and 16-character Google App Password to continue.")
        st.code("Example .env values:\nGMAIL_ADDRESS=you@gmail.com\nGMAIL_APP_PASSWORD=your-16-char-app-password")
        return

    with st.spinner("Connecting to Gmail and processing inbound fax messages..."):
        faxes, logs = fetch_inbound_faxes(email_address, app_password)

    if show_debug:
        st.sidebar.subheader("Debug Logs")
        st.sidebar.code("\n".join(logs) if logs else "No debug output yet.")

    if not faxes:
        st.info("No inbound faxes were found. Check the debug logs if the inbox search needs adjustment.")
        return

    faxes = sorted(faxes, key=lambda item: item["received_at"], reverse=True)
    total_faxes = len(faxes)
    today_count = sum(1 for fax in faxes if fax["received_at"].date() == datetime.now().date())

    col1, col2 = st.columns(2)
    col1.metric("Total Faxes Received Today", today_count)
    col2.metric("Total Inbound Faxes", total_faxes)

    page_size = PAGE_SIZE
    start = (st.session_state.current_page - 1) * page_size
    end = start + page_size
    visible_faxes = faxes[start:end]

    st.subheader("Received Faxes")
    st.write(f"Showing {start + 1}–{min(end, total_faxes)} of {total_faxes} inbound faxes")

    for index, fax in enumerate(visible_faxes, start=start):
        with st.container():
            left, right = st.columns([1.3, 1])
            with left:
                if fax["thumb_path"].exists():
                    st.image(str(fax["thumb_path"]), use_container_width=True)
            with right:
                st.markdown(f"**{fax['company_name']}**")
                st.write(f"Received: {fax['received_at'].strftime('%Y-%m-%d %H:%M:%S')}")
                st.write(f"Caller ID: {fax['caller_id']}")
                st.write(f"PDF: {fax['pdf_path'].name}")
                st.download_button(
                    label="Download PDF",
                    data=fax["pdf_path"].read_bytes(),
                    file_name=fax["pdf_path"].name,
                    mime="application/pdf",
                    key=f"download_{index}",
                )
            st.divider()

    if end < total_faxes:
        if st.button("Show more", key="show_more"):
            st.session_state.current_page += 1
            st.experimental_rerun()

    st.markdown(
        "---\n"
        "This dashboard scans all inbound eFax messages from Gmail and downloads the PDF attachments locally. "
        "It does not limit matched inbound eFax files, and it uses pagination so the page stays responsive. "
        "Saved contact numbers show the contact name instead of Unknown Sender."
    )


if __name__ == "__main__":
    main()
