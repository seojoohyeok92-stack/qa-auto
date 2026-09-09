# Golden human-review queue

각 항목은 '프로그램이 무엇을 했는가'가 아니라 '무엇이 옳은가'를 묻는다.
판단이 서지 않으면 비워 두는 것이 정답이다 -- 빈 칸은 NOT_YET_LABELED로
미판정 case: 13 / 16
보고되고, 추측으로 채운 값은 그대로 정답 취급되어 다음 비교를 오염시킨다.

## Q688159391  (ANCHOR)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 이동식 거치대
- question: 테)이 제품은 리모컨으로 조작하는 제품인가요? 기본 구성품에 리모컨이 들어있는지도 궁금합니다.
- stratum: action=PRODUCT_SPEC identity=NOT_FOUND atoms=2 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q325627544  (CORE)
- product: 삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형
- question: 배송전에 연락주시는거죠? 어머니집으로 배송하는거라 미리 연락 꼭 주셔야합니다.
- stratum: action=NOTIFICATION_POLICY identity=EXACT atoms=1 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: 안녕하세요 오제앤에스 입니다. 구매하신 제품의 설치예정일은 9월 9일 입니다. 설치 전날 저녁 시간대에 수취인 번호로 설치 기사님이 연락하시어 유선 상으로 시간 조율 하에 설치일 설치 방문 하시며 잘못된 연락처 또는 부재중으로 인해 수취인과 연락이 되지 않을 경우 납기일이 하루씩 미뤄지실 수 있으신 점 양해 부탁 드립니다.

## Q325681700  (CORE)
- product: [직접설치]OB-MASHB / 블랙
- question: 더 싼곳을 발견
- stratum: action=CANCEL_RETURN identity=NOT_FOUND atoms=1 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: 안녕하세요 오제앤에스 입니다. 취소 및 반품 요청 주시면 확인 후에 처리 도와드리겠습니다.

## Q687932815  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)이 제품 설치는 제가 직접 하는 건가요? 아니면 삼성 쪽에서 기사님이 오셔서 설치해주시나요?
- stratum: action=INSTALLATION_METHOD identity=NOT_FOUND atoms=1 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: ♣♧안녕하세요♧♣ 오제 챗봇(Chat Bot)이 답변드립니다. 해당 상품은 고객님이 직접 설치하는 방식이 아니라, 삼성 기사님이 방문하여 설치해 드리는 상품입니다. 안내드린 내용이 문의하신 내용과 다른 경우, 네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다. 감사합니다.

## Q687932860  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)이 제품 4K UHD 맞나요? 그리고 설치는 삼성 기사님이 방문해서 해주시는 건가요?
- stratum: action=PRODUCT_SPEC identity=NOT_FOUND atoms=2 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q687932894  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)예전에 쓰던 TV처럼 RF 케이블을 벽에 연결해서 바로 방송을 볼 수 있나요? 별도 셋톱박스가 필요한지도 궁금하고, 집에서 사용하는 제품인데 구매해도 되는지도 알려주세요.
- stratum: action=PRODUCT_SPEC identity=NOT_FOUND atoms=3+ need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q687981328  (CORE)
- product: 삼성 4K UHD TV 스마트 비즈니스TV 1등급 티비 기사님 방문설치 189.3cm(75인치), 스탠드형
- question: 삼성 4K UHD TV 스마트 비즈니스TV 1등급 티비 기사님 방문설치 189.3cm(75인치), 스탠드형 1. 스탠드 다리사이 간격이 확인해보니 1250(넓게), 793(좁게) 로 확인됩니다만, 설치환경상 회의데스크에 설치해야되고 793(좁게)로 세워야 할거같은데 이 좁은폭으로 확실히 설치 가능한지, 확인 부탁드립니다 2. 이번주중에 구매할시에 예상설치 
- stratum: action=INSTALLATION_METHOD identity=EXACT atoms=3+ need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: 안녕하세요 오제앤에스 입니다. 삼성서비스센터를 통해 전달받은 규격은 1250mm, 793mm 가 맞습니다. 구매일로부터 약 1주일 정도 소요되실 수 있으시며 설치 전날 저녁 시간대에 설치 기사님께서 수취인 번호로 연락하시어 시간 조율 후 익일 방문하십니다. 기본적으로 별도로 부과되는 설치비는 없습니다.

## Q687992357  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)구매하면 기본으로 같이 오는 구성품이 어떤 것들이 있나요? 따로 준비해야 하는 것도 있는지 궁금합니다.
- stratum: action=PACKAGE_CONTENTS identity=NOT_FOUND atoms=2 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q687992384  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)사용하다가 화면이나 제품에 문제가 생기면 어디로 접수하면 되나요? 무상으로 서비스 받을 수 있는 기간도 궁금해요.
- stratum: action=REPAIR identity=NOT_FOUND atoms=2 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q687992413  (CORE)
- product: 삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트
- question: 테)지금 쓰고 있는 오래된 제품이 하나 있는데 새 제품 받을 때 같이 가져가 주실 수 있을까요? 별도로 신청해야 하나요?
- stratum: action=COLLECTION identity=NOT_FOUND atoms=2 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q688012021  (CORE)
- product: 삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형
- question: 지역 케이블연결때문에 문의 드립니다 튜너있는지? 그리고 지금 부모님 방에 40인치 벽걸이 티비가 고장나서 바꾸려고 하는데 기존 벽걸이 크라켓이 호환될지 안될지가 걱정이긴한데 지금 여기서 주문안하면 설기기사님이 여분으로 들고 다니시는게 있는지도 궁굼합니다 기존 크라켓은 10년정도 된 거고 집에 사용했던 티비도 삼성티비입니다 또 호환이 됐을시 기존 크라켓이 너
- stratum: action=PRODUCT_SPEC identity=EXACT atoms=3+ need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: 안녕하세요. 고객님~ RF 동축케이블 있는 제품으로 유선 연결해서 사용 가능합니다. 구매시 추가 구성 벽걸암 추가로 구매를 해주시고, 기사님 방문시 기존 벽걸이암 호환여부 확인후 기존 벽걸이암 제품에 설치가 되신다고 하시면 기존 벽걸이 사용하시고 추가로 구매하신 암은 부분 반품으로 진행 가능합니다. 기사님게서 브라켓을 여분으로 들고 다니지는 않습니다.

## Q688055579  (CORE)
- product: 삼성 삼탠바이미 스마트 M5 80cm(32인치)IPTV 모니터 화이트+스탠드 2in1거치대
- question: 스마트허브 업데이트중입니다 라고 뜨고 넷플릭스 유튜브 사용을 못하고 있습니다 어떻게 하면 되나요?
- stratum: action=REPAIR identity=EXACT atoms=1 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)

## Q688081748  (CORE)
- product: 삼탠배터리 VIPB01W 무탠바이미 무선 삼탠바이미 만들기 결합식 배터리 이동식거치대 삼성 M5 80.1cm TV모니터 스탠드
- question: 안녕하세요 혹시 무빙스타일 M7 107.9cm 라이트 (SKULS43FM703U-1WE) 호환되나요?
- stratum: action=PRODUCT_SPEC identity=NOT_FOUND atoms=1 need_order=False need_dps=False
- unlabelled: expected_answerability, expected_review_required, expected_order_lookup, expected_dps_lookup, expected_answer_quality
- 실제 판매자 답변: 안녕하세요 고객님 무빙스타일 제품 및 43인치 모델과는 호환되지않습니다. 감사합니다.
