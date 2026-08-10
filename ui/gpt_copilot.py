from __future__ import annotations

import json
from typing import Any

import streamlit as st

from repositories.database import Database
from repositories.gpt_chat_repository import GptChatRepository
from repositories.project_knowledge_repository import ProjectKnowledgeRepository
from services.gpt_copilot_service import GptCopilotService
from services.gpt_chat_import_service import GptChatImportService
from ui.session_identity import current_identity


def _display_session(session: dict[str, Any]) -> str:
    user = str(session.get("user_name") or "-")
    title = str(session.get("title") or "새 대화")
    return f"#{session['id']} · {title} · {user}"


def _chat_export_text(uploaded: Any) -> tuple[str, str]:
    name = str(getattr(uploaded, "name", "chat-export"))
    raw = uploaded.getvalue()
    text = raw.decode("utf-8", errors="replace")
    if not name.lower().endswith(".json"):
        return name, text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return name, text

    lines: list[str] = []
    conversations = payload if isinstance(payload, list) else [payload]
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        conv_title = str(conversation.get("title") or "ChatGPT 대화")
        mapping = conversation.get("mapping")
        if isinstance(mapping, dict):
            convo_lines: list[str] = []
            nodes = sorted(
                mapping.values(),
                key=lambda node: (
                    ((node or {}).get("message") or {}).get("create_time") or 0
                    if isinstance(node, dict)
                    else 0
                ),
            )
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                message = node.get("message")
                if not isinstance(message, dict):
                    continue
                author = message.get("author") or {}
                role = str(author.get("role") or "unknown") if isinstance(author, dict) else "unknown"
                content = message.get("content") or {}
                parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list):
                    body = "\n".join(str(part) for part in parts if isinstance(part, (str, int, float))).strip()
                    if body:
                        convo_lines.append(f"{role}: {body}")
            if convo_lines:
                lines.append(f"## {conv_title}\n" + "\n\n".join(convo_lines))
        else:
            lines.append(f"## {conv_title}\n{json.dumps(conversation, ensure_ascii=False, default=str)}")
    return name, "\n\n".join(lines) if lines else text


def _render_assistant_message(content: str) -> None:
    marker = "## 기술 정보"
    if marker not in content:
        marker = "기술 정보" if "\n기술 정보" in content else ""
    if not marker:
        st.markdown(content)
        return
    body, technical = content.split(marker, 1)
    st.markdown(body.strip())
    with st.expander("기술 정보", expanded=False):
        st.markdown(technical.strip())


def render_gpt_copilot(database: Database) -> None:
    service = GptCopilotService(database)
    chats = GptChatRepository(database)
    knowledge = ProjectKnowledgeRepository(database)
    status = service.status()
    identity = current_identity()
    username = str(identity.get("username") or "local-user")
    selected_inquiry_id = st.session_state.get("selected_inquiry_id")
    selected_inquiry_id = (
        int(selected_inquiry_id)
        if selected_inquiry_id not in (None, "")
        else None
    )
    st.markdown(
        """
        <style>
        [data-testid="stChatMessage"] p,
        [data-testid="stChatMessage"] li { color: #f2f6fb !important; opacity: 1 !important; }
        [data-testid="stChatMessage"] code { color: #d9ecff !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    label = (
        f"🤖 GPT 운영 도우미 · {status['model']} · "
        f"Project Knowledge {status['knowledge_count']}건"
    )
    with st.expander(label, expanded=False):
        top_a, top_b, top_c = st.columns([1.1, 3.8, 2.1], gap="small")
        if top_a.button("새 대화", key="gpt_copilot_new_chat", width="stretch"):
            session = chats.create_session(
                user_name=username,
                inquiry_id=selected_inquiry_id,
            )
            st.session_state["gpt_copilot_session_id"] = int(session["id"])
            st.session_state["gpt_copilot_session_select"] = int(session["id"])
            st.rerun()

        session_search = top_b.text_input(
            "과거 대화 검색", key="gpt_copilot_session_search",
            placeholder="대화 제목 또는 사용자 검색",
            label_visibility="collapsed",
        )
        all_sessions = chats.list_sessions(limit=200)
        sessions = list(all_sessions)
        if str(session_search or "").strip():
            needle = str(session_search).strip().lower()
            sessions = [
                item for item in sessions
                if needle in f"{item.get('title','')} {item.get('user_name','')}".lower()
            ]
            if not sessions:
                top_b.caption("검색 결과가 없어 최근 대화를 표시합니다.")
                sessions = list(all_sessions)
        current_session_id = st.session_state.get("gpt_copilot_session_id")
        if current_session_id is None and sessions:
            current_session_id = int(sessions[0]["id"])
            st.session_state["gpt_copilot_session_id"] = current_session_id
        if current_session_id is None:
            session = chats.create_session(
                user_name=username,
                inquiry_id=selected_inquiry_id,
            )
            current_session_id = int(session["id"])
            st.session_state["gpt_copilot_session_id"] = current_session_id
            sessions = chats.list_sessions(limit=80)

        session_ids = [int(item["id"]) for item in sessions]
        if int(current_session_id) not in session_ids and session_ids:
            current_session_id = session_ids[0]
        selected_session = top_b.selectbox(
            "대화 기록",
            options=session_ids,
            index=session_ids.index(int(current_session_id)) if session_ids else 0,
            format_func=lambda value: _display_session(
                next(item for item in sessions if int(item["id"]) == int(value))
            ),
            key="gpt_copilot_session_select",
            label_visibility="collapsed",
        )
        if int(selected_session) != int(current_session_id):
            st.session_state["gpt_copilot_session_id"] = int(selected_session)
            st.rerun()

        provider_text = (
            "GPT READY"
            if status["ready"]
            else "GPT 설정 확인 필요"
        )
        top_c.metric("Copilot", provider_text)
        if not status["ready"]:
            st.warning(
                " / ".join(status["issues"])
                or "실제 GPT Provider가 활성화되지 않았습니다. 대화 기록과 Project Knowledge는 계속 저장됩니다."
            )

        context_cols = st.columns(5, gap="small")
        include_inquiry = context_cols[0].toggle(
            "현재 문의 참조",
            value=True,
            key="gpt_copilot_include_inquiry",
            disabled=selected_inquiry_id is None,
        )
        include_learning = context_cols[1].toggle(
            "Learning 참조",
            value=True,
            key="gpt_copilot_include_learning",
            disabled=selected_inquiry_id is None,
        )
        include_knowledge = context_cols[2].toggle(
            "프로젝트 지식 참조",
            value=True,
            key="gpt_copilot_include_knowledge",
        )
        include_past_chats = context_cols[3].toggle(
            "과거 GPT 대화 참조",
            value=True,
            key="gpt_copilot_include_past_chats",
        )
        include_historical = context_cols[4].toggle(
            "과거 사례 참조",
            value=True,
            key="gpt_copilot_include_historical",
        )
        if selected_inquiry_id is not None:
            st.caption(f"현재 연결 문의: 내부 Inquiry ID {selected_inquiry_id}")
        else:
            st.caption("현재 선택 문의 없음 · 프로젝트 전체 질문 모드")

        messages = chats.messages(int(selected_session), limit=120)
        chat_box = st.container(height=430, border=True)
        with chat_box:
            if not messages:
                st.caption(
                    "Q&A Auto 설계, 현재 문의, DPS/주문조회, Auto Sync/Auto Post, "
                    "Learning에 대해 질문할 수 있습니다."
                )
            for message in messages:
                role = str(message.get("role") or "assistant")
                with st.chat_message("user" if role == "user" else "assistant"):
                    content = str(message.get("content") or "")
                    if role == "user":
                        st.markdown(content)
                    else:
                        _render_assistant_message(content)

        prompt = st.text_area(
            "GPT에게 질문",
            key="gpt_copilot_prompt",
            height=90,
            placeholder="예: 이 문의가 왜 DPS를 건너뛰었는지 현재 상태를 기준으로 설명해줘.",
        )
        send_col, info_col = st.columns([1.2, 5.8], gap="small")
        if send_col.button(
            "전송",
            type="primary",
            width="stretch",
            key="gpt_copilot_send",
            disabled=not bool(str(prompt or "").strip()),
        ):
            with st.spinner("GPT 운영 도우미가 확인 중입니다..."):
                service.ask(
                    session_id=int(selected_session),
                    message=str(prompt),
                    inquiry_id=selected_inquiry_id,
                    include_inquiry=bool(include_inquiry),
                    include_learning=bool(include_learning),
                    include_knowledge=bool(include_knowledge),
                    include_past_chats=bool(include_past_chats),
                    include_historical=bool(include_historical),
                )
            st.rerun()
        info_col.caption(
            "GPT 운영 도우미는 읽기/설명 전용입니다. 네이버 등록과 자동화 상태를 직접 변경하지 않습니다."
        )

        with st.expander("Project Knowledge / 과거 ChatGPT 대화 가져오기", expanded=False):
            st.caption(
                "현재 대화에는 Q&A Auto 핵심 설계 결정 요약이 기본 지식으로 포함되어 있습니다. "
                "ChatGPT에서 내보낸 conversations.json 또는 TXT를 추가하면 과거 대화도 검색 참고자료로 저장됩니다."
            )
            upload = st.file_uploader(
                "ChatGPT 대화 파일",
                type=["json", "txt"],
                key="gpt_copilot_chat_import",
            )
            if upload is not None:
                name, text = _chat_export_text(upload)
                st.caption(f"가져오기 준비: {name} · {len(text):,}자")
                if st.button(
                    "Project Knowledge에 저장",
                    key="gpt_copilot_import_button",
                ):
                    result = GptChatImportService(database).import_bytes(
                        file_name=name, raw=upload.getvalue(), user_name=username,
                    )
                    if result.get("duplicate"):
                        st.info("같은 파일은 이미 가져왔습니다. 중복 대화는 생성하지 않았습니다.")
                    else:
                        st.success(
                            "과거 대화를 대화 기록 {sessions}개와 Project Knowledge {chunks}개 조각으로 저장했습니다.".format(
                                sessions=result.get("sessions_created", 0),
                                chunks=result.get("knowledge_chunk_count", 0),
                            )
                        )
                    st.rerun()
