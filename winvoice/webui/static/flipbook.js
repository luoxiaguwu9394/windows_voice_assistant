/* WinVoice 翻页书引擎(第二版:GSAP 驱动)。
 *
 * 交互架构整体搬自 howtotalktowhitekidsaboutracism.com:一条 progress 时间线,
 * 每张纸翻转占 1 单位,拖拽实时 scrub、箭头/键盘 tween 跳页。原站用
 * GSAP timeline + WebGL;这里页面是 DOM(表单要活着),所以 GSAP 只驱动
 * CSS rotateY,时长与缓动沿用原站实测值(跳页 1.5s power3.inOut、
 * 封面 hover 微开、拖拽松手吸附)。GSAP 本体在 vendor/,可自由使用。
 */

"use strict";

const Flipbook = (() => {
  const SHEET_COUNT = 5;
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  const clamp = (v, low, high) => Math.max(low, Math.min(high, v));

  let sheets = [];
  let book, stage;
  let pageWidth = 420;
  const state = { progress: 0 };      // GSAP tween 的目标对象(原站 flipTimeline 同型)
  let drag = null;

  function applyProgress() {
    for (let i = 0; i < sheets.length; i++) {
      const t = clamp01(state.progress - i);
      sheets[i].style.transform = `rotateY(${(t * -180).toFixed(2)}deg)`;
      sheets[i].style.setProperty("--flip-shade", (Math.sin(t * Math.PI) * 0.45).toFixed(3));
      // 堆叠:右侧未翻的纸 i 小者在上;左侧已翻的后翻者在上;翻动中的压住所有人
      const flipping = t > 0.001 && t < 0.999;
      sheets[i].style.zIndex = flipping ? (SHEET_COUNT + 10)
        : (t >= 0.999 ? 10 + i : SHEET_COUNT - i);
    }
    book.dataset.state = state.progress < 0.5 ? "closed" : "open";
    const left = document.getElementById("nav-left");
    const right = document.getElementById("nav-right");
    if (left) left.style.visibility = state.progress > 0.03 ? "visible" : "hidden";
    if (right) right.style.visibility = state.progress < SHEET_COUNT - 0.97 ? "visible" : "hidden";
  }

  function turnTo(page) {
    page = clamp(page, 0, SHEET_COUNT);
    if (Math.abs(page - state.progress) < 0.002) return;
    // 原站实测:箭头/菜单跳页 1.5s power3.inOut
    gsap.to(state, {
      progress: page,
      duration: Math.min(1.7, 0.55 + 0.42 * Math.abs(page - state.progress)),
      ease: "power3.inOut",
      onUpdate: applyProgress,
      onComplete: applyProgress,
    });
  }

  /* ── 拖拽擦洗(原站:拖动 = 擦洗时间线,松手吸附)──────────── */

  function onPointerDown(event) {
    if (event.target.closest("input, textarea, select, button, label, a, .modal")) return;
    if (state.progress < 0.05) return;  // 封面合着时只响应点击翻开,不进入擦洗
    gsap.killTweensOf(state);
    drag = { startX: event.clientX, startProgress: state.progress };
    book.setPointerCapture(event.pointerId);
    book.classList.add("dragging");
  }

  function onPointerMove(event) {
    // buttons === 0 的 pointermove 是合成/悬停移动(capture 会把它重定向到书),
    // 绝不能参与擦洗——一次 (0,0) 处的合成移动曾把书"擦"出去三页(实测)。
    if (!drag || event.buttons === 0) { if (!event.buttons) drag = null; return; }
    state.progress = clamp(drag.startProgress + (drag.startX - event.clientX) / pageWidth, 0, SHEET_COUNT);
    applyProgress();
  }

  function onPointerUp(event) {
    if (!drag) return;
    const vx = (event.clientX - drag.startX);
    const bias = clamp(-vx / pageWidth * 0.6, -0.49, 0.49);
    const target = clamp(Math.round(state.progress + bias), 0, SHEET_COUNT);
    drag = null;
    book.classList.remove("dragging");
    turnTo(target);
  }

  function fitStage() {
    pageWidth = sheets[0].getBoundingClientRect().width || pageWidth;
    stage.style.setProperty("--book-w", pageWidth + "px");
    const scale = Math.min(1, (window.innerWidth - 32) / 980, (window.innerHeight - 32) / 700);
    stage.style.transform = scale < 1 ? `scale(${scale.toFixed(3)})` : "";
  }

  function init() {
    book = document.getElementById("book");
    stage = document.getElementById("stage");
    sheets = [...book.querySelectorAll(".sheet")];
    fitStage();
    applyProgress();

    book.addEventListener("pointerdown", onPointerDown);
    book.addEventListener("pointermove", onPointerMove);
    book.addEventListener("pointerup", onPointerUp);
    book.addEventListener("pointercancel", onPointerUp);

    document.getElementById("nav-left").addEventListener("click", () => turnTo(Math.round(state.progress) - 1));
    document.getElementById("nav-right").addEventListener("click", () => turnTo(Math.round(state.progress) + 1));

    document.addEventListener("keydown", (event) => {
      if (event.target.closest("input, textarea, select")) return;
      if (event.key === "ArrowRight") turnTo(Math.round(state.progress) + 1);
      if (event.key === "ArrowLeft") turnTo(Math.round(state.progress) - 1);
    });

    // 封面:合着时点击翻开(原站进场动作的简化版)
    book.querySelector(".cover").addEventListener("click", () => {
      if (state.progress < 0.5) turnTo(1);
    });
    // 封面 hover 微开(原站:PI/32 的开缝)
    book.querySelector(".cover").addEventListener("mouseenter", () => {
      if (state.progress < 0.05 && !gsap.isTweening(state)) {
        gsap.to(state, { progress: 0.045, duration: 0.5, ease: "power2.out",
                         onUpdate: applyProgress, overwrite: "auto" });
      }
    });
    book.querySelector(".cover").addEventListener("mouseleave", () => {
      if (state.progress < 0.05 && state.progress > 0 && !gsap.isTweening(state)) {
        gsap.to(state, { progress: 0, duration: 0.7, ease: "power2.inOut",
                         onUpdate: applyProgress, overwrite: "auto" });
      }
    });

    const close = document.getElementById("close-book");
    if (close) close.addEventListener("click", () => turnTo(0));

    window.addEventListener("resize", fitStage);

    const api = { turnTo, sheetCount: SHEET_COUNT, get progress() { return state.progress; } };
    window.Flipbook = api;
    return api;
  }

  return { init };
})();
