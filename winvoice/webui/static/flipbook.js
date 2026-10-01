/* WinVoice 翻页书引擎(第三版:GSAP + 居中开书 + 连续落影)。
 *
 * 交互架构仿 howtotalktowhitekidsaboutracism.com:一条 progress 时间线
 * (每张纸翻转占 1 单位),拖拽实时 scrub、箭头/键盘 tween 跳页;GSAP 本体
 * 在 vendor/(原站同款库)。本版新增:
 *  - 合上的书居中;点开后先平移到书脊对中,再翻开(progress 与 shift 同一时间线);
 *  - 落影连续:翻动的纸投在下一张纸上的阴影按翻页角度实时计算(= sin 曲线),
 *    与落定状态无缝衔接,不再突变;
 *  - pointermove 要求 buttons !== 0:pointer capture 会把合成 hover 事件
 *    重定向进拖拽路径,一次 (0,0) 合成移动曾把书"擦"出去三页(实测)。
 */

"use strict";

const Flipbook = (() => {
  const SHEET_COUNT = 6;
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  const clamp = (v, low, high) => Math.max(low, Math.min(high, v));

  let sheets = [];
  let book, stage;
  let pageWidth = 420;
  let scale = 1;                       // 窄窗整体缩放比(笔动画要用)
  // motion 双轨:progress 翻页,shift 平移(合上时书居中 = -pageWidth/2)
  const motion = { progress: 0, shift: 0 };
  let drag = null;

  function applyProgress() {
    for (let i = 0; i < sheets.length; i++) {
      const own = clamp01(motion.progress - i);
      // 翻动中的纸:主翻转 + 自然抖动(幅度被 sin 包络,起翻/落定时归零)
      const flipping = own > 0.001 && own < 0.999;
      let tf = `rotateY(${(own * -180).toFixed(2)}deg)`;
      if (flipping) {
        const envelope = Math.sin(own * Math.PI);
        const wobble = Math.sin(own * Math.PI * 3.5) * 1.6 * envelope;
        const lift = Math.sin(own * Math.PI * 2) * 5;
        tf += ` rotateZ(${wobble.toFixed(2)}deg) translateZ(${lift.toFixed(1)}px)`;
      }
      sheets[i].style.transform = tf;
      // 落影 = 自己翻动的主阴影 与 上一张纸翻过来投在自己身上的影,取强者。
      // 两者都是 sin 曲线,落定时自然归零,与静止状态无缝衔接。
      const cast = clamp01(motion.progress - (i - 1));
      const shade = Math.max(Math.sin(own * Math.PI), Math.sin(cast * Math.PI) * 0.6);
      sheets[i].style.setProperty("--flip-shade", shade.toFixed(3));
      sheets[i].style.zIndex = flipping ? (SHEET_COUNT + 10)
        : (own >= 0.999 ? 10 + i : SHEET_COUNT - i);
    }
    book.style.transform = `translateX(${motion.shift.toFixed(1)}px)`;
    stage.style.setProperty("--progress", motion.progress.toFixed(3));
    book.dataset.state = motion.progress < 0.5 ? "closed" : "open";
    const left = document.getElementById("nav-left");
    const right = document.getElementById("nav-right");
    if (left) left.style.visibility = motion.progress > 0.03 ? "visible" : "hidden";
    if (right) right.style.visibility = motion.progress < SHEET_COUNT - 0.97 ? "visible" : "hidden";
  }

  function turnTo(page) {
    page = clamp(page, 0, SHEET_COUNT);
    if (Math.abs(page - motion.progress) < 0.002) return;
    // 原站实测:跳页 1.5s power3.inOut
    gsap.to(motion, {
      progress: page,
      duration: Math.min(1.7, 0.55 + 0.42 * Math.abs(page - motion.progress)),
      ease: "power3.inOut",
      onUpdate: applyProgress,
      onComplete: applyProgress,
    });
  }

  // 合上/打开:progress 与 shift 走同一条时间线 —— 书先归位再合上(或反之)。
  // onUpdate 必须挂在时间线上:两个子 tween 各自只改数值,画面统一在这里重绘
  // (漏掉它,progress 走完了画面却冻在原地——实测踩过)。
  function setOpen(open) {
    const targetProgress = open ? 1 : 0;
    const targetShift = open ? 0 : -pageWidth / 2;
    gsap.timeline({ onUpdate: applyProgress, onComplete: applyProgress })
      .to(motion, { shift: targetShift, duration: 1.0, ease: "power2.inOut" }, 0)
      .to(motion, { progress: targetProgress, duration: 1.35, ease: "power3.inOut" }, open ? 0.35 : 0.1);
  }

  /* ── 拖拽擦洗 ─────────────────────────────────────────────── */

  function onPointerDown(event) {
    if (event.target.closest("input, textarea, select, button, label, a, .modal")) return;
    if (motion.progress < 0.05) return;  // 封面合着时只响应点击翻开
    gsap.killTweensOf(motion);
    drag = { startX: event.clientX, startProgress: motion.progress };
    book.setPointerCapture(event.pointerId);
    book.classList.add("dragging");
  }

  function onPointerMove(event) {
    // buttons === 0 的 pointermove 是合成/悬停移动,绝不能参与擦洗
    if (!drag || event.buttons === 0) { if (!event.buttons) drag = null; return; }
    motion.progress = clamp(drag.startProgress + (drag.startX - event.clientX) / pageWidth, 0, SHEET_COUNT);
    applyProgress();
  }

  function onPointerUp(event) {
    if (!drag) return;
    const vx = (event.clientX - drag.startX);
    const bias = clamp(-vx / pageWidth * 0.6, -0.49, 0.49);
    const target = clamp(Math.round(motion.progress + bias), 0, SHEET_COUNT);
    drag = null;
    book.classList.remove("dragging");
    turnTo(target);
  }

  function fitStage() {
    // offsetWidth = 未变换的布局宽;getBoundingClientRect 会把舞台缩放算进去,
    // 而且 --book-w 既参与布局又被测量,用 rect 会形成自反馈收缩循环。
    pageWidth = sheets[0].offsetWidth || pageWidth;
    stage.style.setProperty("--book-w", pageWidth + "px");
    scale = Math.min(1, (window.innerWidth - 32) / 980, (window.innerHeight - 32) / 700);
    stage.style.transform = scale < 1 ? `scale(${scale.toFixed(3)})` : "";
    // 合上态的居中平移量跟随页宽
    if (motion.progress < 0.05) { motion.shift = -pageWidth / 2; applyProgress(); }
    window.__stageScale = scale;
  }

  function init() {
    book = document.getElementById("book");
    stage = document.getElementById("stage");
    sheets = [...book.querySelectorAll(".sheet")];
    fitStage();
    motion.shift = -pageWidth / 2;   // 合上的书居中
    applyProgress();

    book.addEventListener("pointerdown", onPointerDown);
    book.addEventListener("pointermove", onPointerMove);
    book.addEventListener("pointerup", onPointerUp);
    book.addEventListener("pointercancel", onPointerUp);

    document.getElementById("nav-left").addEventListener("click", () => turnTo(Math.round(motion.progress) - 1));
    document.getElementById("nav-right").addEventListener("click", () => turnTo(Math.round(motion.progress) + 1));

    document.addEventListener("keydown", (event) => {
      if (event.target.closest("input, textarea, select")) return;
      if (event.key === "ArrowRight") turnTo(Math.round(motion.progress) + 1);
      if (event.key === "ArrowLeft") turnTo(Math.round(motion.progress) - 1);
    });

    // 封面:点击 → 向中间平移 + 翻开;hover 微开一缝(原站 PI/32)。
    // hover tween 与点击都会动 motion:点击必须先杀掉 hover tween,否则
    // isTweening 为真会把打开动作整个跳过(实测:点封面没反应)。
    book.querySelector(".cover").addEventListener("click", () => {
      if (motion.progress < 0.5) {
        gsap.killTweensOf(motion);
        setOpen(true);
      }
    });
    book.querySelector(".cover").addEventListener("mouseenter", () => {
      if (motion.progress < 0.05 && !gsap.isTweening(motion)) {
        gsap.to(motion, { progress: 0.045, duration: 0.5, ease: "power2.out",
                          onUpdate: applyProgress, overwrite: "auto" });
      }
    });
    book.querySelector(".cover").addEventListener("mouseleave", () => {
      if (motion.progress < 0.05 && motion.progress > 0 && !gsap.isTweening(motion)) {
        gsap.to(motion, { progress: 0, duration: 0.7, ease: "power2.inOut",
                          onUpdate: applyProgress, overwrite: "auto" });
      }
    });

    const close = document.getElementById("close-book");
    if (close) close.addEventListener("click", () => setOpen(false));

    window.addEventListener("resize", fitStage);

    const api = {
      turnTo,
      open: () => setOpen(true),
      sheetCount: SHEET_COUNT,
      get progress() { return motion.progress; },
      get scale() { return scale; },
    };
    window.Flipbook = api;
    return api;
  }

  return { init };
})();
