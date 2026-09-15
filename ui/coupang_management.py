from __future__ import annotations
import streamlit as st
from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS, get_coupang_accounts, get_coupang_account
from api.coupang_read_client import CoupangReadClient
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import CoupangProductMappingRepository
from repositories.product_catalog_repository import ProductCatalogRepository, canonical_model_identity
from services.coupang_product_mapping_service import CoupangProductMappingService
from services.coupang_product_catalog_sync_service import CoupangProductCatalogSyncService

NAMES={"ALL":"전체",COUPANG_OJE_NS:"오제앤에스",COUPANG_OJE_PLUS:"오제플러스"}
ACCOUNT_CODES=(COUPANG_OJE_NS,COUPANG_OJE_PLUS)
# The operational matching screen intentionally has no cross-account "ALL"
# view.  Keeping the existing selector shape while narrowing its source avoids
# accidental inter-account catalog mixing.
NAMES={code:NAMES[code] for code in ACCOUNT_CODES}

# This is an administrative Coupang UI allowlist, not a Product Catalog or
# Product Knowledge restriction.  Stored values continue to use the shared
# canonical_model_identity() convention.
MANUAL_MODEL_CODES=(
    "LS25BG400EKXKR", "LS25HG400EKXKR", "LS27HG400EKXKR",
    "LS32FG500EKXKR", "LS32DM500EKXKR", "LS32DM501EKXKR",
    "LS27FM500EKXKR", "LS27FM501EKXKR", "LS32FM500EKXKR",
    "LS32FM501EKXKR", "LS27DG700EKXKR", "LS27FG700EKXKR",
    "LS27HG806EFXKR", "LS32HG806ESXKR", "LS49CG954EKXKR",
    "LS49DG930SKXKR", "LS32DG300EKXKR", "LS22D400GAKXKR",
    "LS24D400GAKXKR", "LS27D400GAKXKR", "LH43BEHHLGFXKR",
    "LH50BEHHLGFXKR", "LH85BEHHLGFXKR",
)

def _models() -> list[str]:
    repo=ProductCatalogRepository(); data=repo.catalog(); aliases=data.get('aliases') or {}
    keys=[*data.get('catalog',{}).keys()]
    knowledge=repo.product_knowledge()
    keys.extend(
        row.get('model_code') for row in knowledge.get('model_facts', [])
        if isinstance(row, dict)
    )
    known={canonical_model_identity(key,aliases=aliases) for key in keys}
    return [
        identity for code in MANUAL_MODEL_CODES
        if (identity:=canonical_model_identity(code,aliases=aliases)) in known
    ]

def _display_model_for(canonical_model: object, models: list[str]) -> str | None:
    aliases=ProductCatalogRepository().catalog().get('aliases') or {}
    canonical=str(canonical_model or '').strip()
    return next((code for code in models if canonical_model_identity(code,aliases=aliases)==canonical),None)
def render_coupang_management(database) -> None:
    st.title("쿠팡 관리")
    configured={a.account_code for a in get_coupang_accounts()}
    if st.session_state.get("coupang_admin_account") not in ACCOUNT_CODES:
        st.session_state["coupang_admin_account"]=COUPANG_OJE_NS
    st.caption(" · ".join(f"{NAMES[k]} {'● 연결 설정됨' if k in configured else '○ 설정 안 됨'}" for k in (COUPANG_OJE_NS,COUPANG_OJE_PLUS)))
    account=st.selectbox("계정",list(NAMES),format_func=NAMES.get,key="coupang_admin_account")
    status=st.segmented_control("매칭 상태",["ALL","COMPLETE","REVIEW"],format_func={"ALL":"전체","COMPLETE":"매칭 완료","REVIEW":"수동 매칭 필요"}.get,default="ALL",key="coupang_admin_status") or "ALL"
    catalog=CoupangProductCatalogRepository(database); mappings=CoupangProductMappingRepository(database)
    st.subheader("상품 동기화")
    for code in (COUPANG_OJE_NS,COUPANG_OJE_PLUS):
        if st.button(f"{NAMES[code]} 동기화",key=f"coupang_sync_{code}",disabled=code not in configured):
            a=get_coupang_account(code); client=CoupangReadClient(access_key=a.access_key,secret_key=a.secret_key,vendor_id=a.vendor_id)
            mapping=CoupangProductMappingService(account_code=code,read_client=client,repository=mappings)
            result=CoupangProductCatalogSyncService(account_code=code,read_client=client,catalog_repository=catalog,mapping_service=mapping).sync_account()
            st.success(f"등록상품 {result.products_seen} · 판매옵션 {result.options_seen} · 자동 {result.auto_exact} · 재사용 {result.confirmed_reused} · 검토 {result.needs_review} · 오류 {len(result.errors)}")
    products=catalog.grouped_products(account_code=account,status=status)
    st.subheader("상품 매칭")
    if not products:
        st.info("아직 동기화된 쿠팡 상품이 없습니다. 상품 동기화를 실행하면 판매상품과 판매옵션을 불러옵니다."); return
    models=_models()
    for product in products:
        opts=product['options']; done=sum(x['complete'] for x in opts); title=product.get('seller_product_name') or product.get('display_product_name') or product['seller_product_id']
        with st.expander(f"{title} · {NAMES.get(product['account_code'],product['account_code'])} · 옵션 {len(opts)} · 완료 {done} · 검토 {len(opts)-done}"):
            for option in opts:
                state="자동 매칭 완료" if option['mapping_source']=="AUTO_EXACT" and option['complete'] else ("수동 매칭 완료" if option['complete'] else "수동 매칭 필요")
                st.write(f"**{option.get('item_name') or option['vendor_item_id']}** — 실제 모델: {option['canonical_model'] or '-'} · {state}")
                current=option['canonical_model'] if option['canonical_model'] in models else None
                choice=st.selectbox("실제 모델",[None,*models],index=([None,*models].index(current) if current else 0),format_func=lambda x:x or "모델 선택",key=f"coupang_model_{product['account_code']}_{option['vendor_item_id']}")
                if st.button("매칭 저장",key=f"coupang_save_{product['account_code']}_{option['vendor_item_id']}",disabled=choice is None):
                    service=CoupangProductMappingService(account_code=product['account_code'],read_client=None,repository=mappings) # type: ignore[arg-type]
                    service.save_manual_mapping(vendor_item_id=option['vendor_item_id'],canonical_model=choice,seller_product_id=product['seller_product_id'])
                    st.rerun()
