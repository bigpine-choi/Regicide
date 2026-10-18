# 브라우저 부하 제거 후 LMR 재대국 독립 감사

상태: failed. 검사 13891개, 실패 109개.

새 대국: 4승 6무 6패, 완료 16판, 득점률 43.75%. 미완료 0판은 무승부와 점수 분모에서 제외했다.

기존 대국은 4승6무6패(43.75%)였다. 두 실행은 합산하지 않았으며 결과 차이를 브라우저 부하만의 인과 효과로 해석하지 않는다.

실행 전 20.29초, 대국 중 291.11초와 종료 후 2.07초의 관측에서 일반 브라우저 프로세스 수와 CPU 증가가 모두 0인지 재계산했다. 채택 기준 통과: False.

별도 관측한 내장 WebView2는 대국 중 3.000000 CPU초, 전체 16논리CPU 용량의 평균 0.064408%였다. 해당 프로세스가 존재하므로 모든 웹 렌더러가 없었다고 표현하지 않는다.

PGN 전체 합법 수·무승부 청구와 종료, 고정8시작점/선후공16판, 동일 실행 파일/소스 식별자와 옵션, 각 검색의 전체 이력·선택 수·노드·깊이·시간·LMR 기록을 대조했다. 이전 회귀검사와 고정 깊이 실험은 새로 실행하지 않았다.

8개 고정 시작점의 소표본이다. 결과는 이 조건의 관측이며 일반적인 기력 향상·Elo를 확정하지 않는다. CPU 표본은 짧게 실행된/접근 불가 프로세스, GPU·온도·클럭·예약 작업을 완전히 포착하지 못한다.

실패 항목:

- match sample 459 browser zero
- match sample 460 browser zero
- match sample 461 browser zero
- match sample 462 browser zero
- match sample 463 browser zero
- match sample 464 browser zero
- match sample 465 browser zero
- match sample 466 browser zero
- match sample 467 browser zero
- match sample 468 browser zero
- match sample 469 browser zero
- match sample 470 browser zero
- match sample 471 browser zero
- match sample 472 browser zero
- match sample 473 browser zero
- match sample 474 browser zero
- match sample 475 browser zero
- match sample 476 browser zero
- match sample 477 browser zero
- match sample 478 browser zero
- match sample 479 browser zero
- match sample 480 browser zero
- match sample 481 browser zero
- match sample 482 browser zero
- match sample 483 browser zero
- match sample 484 browser zero
- match sample 485 browser zero
- match sample 486 browser zero
- match sample 487 browser zero
- match sample 488 browser zero
- match sample 489 browser zero
- match sample 490 browser zero
- match sample 491 browser zero
- match sample 492 browser zero
- match sample 493 browser zero
- match sample 494 browser zero
- match sample 495 browser zero
- match sample 496 browser zero
- match sample 497 browser zero
- match sample 498 browser zero
- match sample 499 browser zero
- match sample 500 browser zero
- match sample 501 browser zero
- match sample 502 browser zero
- match sample 503 browser zero
- match sample 504 browser zero
- match sample 505 browser zero
- match sample 506 browser zero
- match sample 507 browser zero
- match sample 508 browser zero
- match sample 509 browser zero
- match sample 510 browser zero
- match sample 511 browser zero
- match sample 512 browser zero
- match sample 513 browser zero
- match sample 514 browser zero
- match sample 515 browser zero
- match sample 516 browser zero
- match sample 517 browser zero
- match sample 518 browser zero
- match sample 519 browser zero
- match sample 520 browser zero
- match sample 521 browser zero
- match sample 522 browser zero
- match sample 523 browser zero
- match sample 524 browser zero
- match sample 525 browser zero
- match sample 526 browser zero
- match sample 527 browser zero
- match sample 528 browser zero
- match sample 529 browser zero
- match sample 530 browser zero
- match sample 531 browser zero
- match sample 532 browser zero
- match sample 533 browser zero
- match sample 534 browser zero
- match sample 535 browser zero
- match sample 536 browser zero
- match sample 537 browser zero
- match sample 538 browser zero
- match sample 539 browser zero
- match sample 540 browser zero
- match sample 541 browser zero
- match sample 542 browser zero
- match sample 543 browser zero
- match sample 544 browser zero
- match sample 545 browser zero
- match sample 546 browser zero
- match sample 547 browser zero
- match sample 548 browser zero
- match sample 549 browser zero
- match sample 550 browser zero
- match sample 551 browser zero
- match sample 552 browser zero
- match sample 553 browser zero
- match sample 554 browser zero
- match sample 555 browser zero
- match sample 556 browser zero
- match sample 557 browser zero
- match sample 558 browser zero
- match sample 559 browser zero
- match sample 560 browser zero
- match sample 561 browser zero
- match sample 562 browser zero
- postflight sample 1 browser zero
- postflight sample 2 browser zero
- postflight sample 3 browser zero
- postflight sample 4 browser zero
- stored browser criterion recomputed
