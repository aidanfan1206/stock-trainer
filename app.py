"""股票短線交易訓練工具 (MWE)

用 yfinance 取得歷史股價，以「下一天」按鈕逐日推進，
模擬「只看得到今天收盤、就要判斷下一步」的短線交易訓練情境。
含模擬買賣、資產看板、買賣點標註與結束日期自動結算。
支援美股、港股（數字自動補全 0700.HK）與 A 股（自動判斷 .SS/.SZ）。

執行方式：
    pip install -r requirements.txt
    streamlit run app.py
"""

import json
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

INITIAL_CASH = 10_000.0  # 初始資金（以所選市場貨幣計）

# 市場 → (貨幣代碼, 貨幣符號, 顯示小數位數)
MARKETS = {
    "美股": ("USD", "$", 2),
    "港股": ("HKD", "$", 3),  # 港股報價慣用 3 位小數
    "A股": ("CNY", "¥", 2),
}


def resolve_ticker(raw: str, market: str):
    """依市場補全 yfinance 代號字尾。

    回傳 (ticker, error_message)；失敗時 ticker 為 None。
    """
    t = raw.strip().upper()
    if market == "美股":
        return t, None
    if "." in t:  # 已含字尾（如 0700.HK / 600519.SS），直接使用
        return t, None
    digits = "".join(c for c in t if c.isdigit())
    if market == "港股":
        if not digits:
            return None, "港股代號需為數字，例如 700 或 0700"
        return f"{digits.zfill(4)[-4:]}.HK", None
    # A股：6 位數字，依開頭判斷交易所
    if len(digits) != 6:
        return None, "A股代號需為 6 位數字，例如 600519 或 000001"
    if digits[0] == "6":
        return f"{digits}.SS", None  # 滬市（含科創板 688）
    if digits[0] in ("0", "3"):
        return f"{digits}.SZ", None  # 深市（含創業板 300）
    return None, f"不支援的 A股代碼開頭「{digits[0]}」：目前支援滬（6 開頭）與深（0/3 開頭）"


st.set_page_config(page_title="短線交易訓練", page_icon="📈",
                   layout="wide")

st.title("📈 股票短線交易訓練工具")
st.caption("MWE：逐日推進歷史股價 → 模擬買賣 → 結束自動結算（美股／港股／A股）")

# 壓縮主容器左右留白＋收斂數字字型，讓圖表與看板更寬敞、金額不被截斷
st.markdown(
    """
    <style>
    html {
        scroll-behavior: smooth;  /* 捲動平順化 */
    }
    .block-container {
        padding-left: 1.5rem !important;
        padding-right: 1.5rem !important;
        max-width: none !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.35rem !important;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# 圖表工具、捲動、縮放與畫線守護（v8）：
# st.markdown 注入的 <script> 經 innerHTML 寫入、瀏覽器不會執行，
# 因此改用 st.html（components.html 的後繼 API，需開啟
# unsafe_allow_javascript）——其 iframe 與應用同源，script 會
# 真正執行，並透過 parent.document 操作應用主文件的圖表。
# 所有狀態存 parent window（跨 iframe 重建保留），並以「最新實例」
# 旗標避免 rerun 後多個輪詢並存。
# v8：新增畫線工具（plotly 內建 drawline／drawrect／drawopenpath
# ／eraseshape 模式列按鈕）。形狀編輯無動畫、現讀現寫：畫完
# 300ms 寫入 shapes_state 持久化（跨 rerun／重新整理保留），
# 點擊按鈕時亦隨該互動同批提交；形狀由圖表規格回寫並設
# editable=True（可繼續拖曳頂點微調）。縮放機制同 v7。
st.html(
    """
    <script>
    // IIFE 包覆：st.html 的 script 都在同一個文件環境執行，
    // 用函式作用域避免 const 宣告在 rerun 重跑時互相衝突
    (function () {
    const P = parent, W = P.window;
    // 找出「應用文件」：本機部署時 parent 就是應用文件；Community
    // Cloud 把 Streamlit 應用包在 /~/+/ 的 iframe 裡，而 st.html
    // 的 iframe 掛在外層頁面——逐一檢查 parent／top 及其子 frame
    // （同源才可存取），取含 stAppViewContainer 的那個。
    const findAppDoc = () => {
      const cands = [];
      const seen = new Set();
      const pushWin = (w) => {
        try {
          if (!w || seen.has(w)) return;
          seen.add(w);
          cands.push(w);
          for (let i = 0; i < w.frames.length; i++) pushWin(w.frames[i]);
        } catch (err) {}
      };
      pushWin(parent);
      try { pushWin(top); } catch (err) {}
      let best = null, bestScore = -1;
      for (const w of cands) {
        try {
          let score = 0;
          if (w.document.querySelector(
              '[data-testid="stAppViewContainer"]')) score += 10;
          score += w.document.querySelectorAll(
            '[data-testid="stTextInput"]').length;
          if (score > bestScore) { bestScore = score; best = w; }
        } catch (err) {}
      }
      return (best || parent).document;
    };
    let D = findAppDoc();
    P.console.log("[kline-guard] v16 installed");
    W.__guardInstance = (W.__guardInstance || 0) + 1;
    const myId = W.__guardInstance;

    // —— 多重事件註冊：document／window／app 容器三層都掛——
    // 某些前端環境下事件可能在到達 document 前被攔截（雲端曾
    // 出現「點主內容按鈕不觸發、點工具列卻觸發」），多層保險
    const onMulti = (handler) => {
      D.addEventListener("click", handler, true);
      P.document.addEventListener("click", handler, true);
      W.addEventListener("click", handler, true);
      try {
        if (top && top.document && top.document !== D &&
            top.document !== P.document) {
          top.document.addEventListener("click", handler, true);
        }
      } catch (err) {}
      const appC = D.querySelector('[data-testid="stAppViewContainer"]');
      if (appC) {
        appC.addEventListener("click", handler, true);
      }
    };

    // —— 捲動位置守護 ——
    W.__savedScroll = W.__savedScroll || 0;
    if (!W.__scrollHook) {
      W.__scrollHook = true;
      const addScrollHook = (win) => {
        win.addEventListener("scroll", () => {
          // 寫入/重建窗口內的捲動不算（focus 等會把頁面拉走）
          if (W.__freezeScroll && Date.now() < W.__freezeScroll) return;
          W.__savedScroll = win.scrollY;
        });
      };
      addScrollHook(W);
      try { addScrollHook(D.defaultView); } catch (err) {}
    }

    // —— 圖表工具（pan/zoom/select/lasso）狀態守護 ——
    // plotly 的 uirevision 不涵蓋 dragmode；Streamlit 每次 rerun
    // 會把 modebar 工具重置回 zoom。capture 攔截 modebar 點擊記錄
    // 「使用者」的選擇（data-attr="dragmode" + data-val），定時比對
    // 圖表實際工具，被重置就點回對應按鈕（plotly 自己的 UI 路徑）
    onMulti((e) => {
      if (W.__guardInstance !== myId) return;  // 舊實例：退位
      const t = e.target && e.target.closest
        ? e.target.closest(".modebar-btn") : null;
      if (!t) return;
        const attr = t.getAttribute("data-attr");
        const val = t.getAttribute("data-val");
        if (attr === "dragmode" &&
            val && ["pan", "zoom", "select", "lasso",
                    "drawline", "drawrect", "drawopenpath",
                    "drawclosedpath", "drawcircle"].includes(val)) {
          W.__savedDragmode = val;
        }
    });

    // —— 定時比對＋回復（只有最新實例的輪詢會持續運作）——
    const iv = setInterval(() => {
      if (W.__guardInstance !== myId) { clearInterval(iv); return; }
      const nd = findAppDoc();
      if (nd && nd !== D) { D = nd; }  // 應用 frame 若重載：換新文件
      // B 站圖標注入（登入版雲端外框才有的 GitHub 連結旁）
      if (!W.__biliDone) addBiliIcon();
      const saved = W.__savedDragmode;
      const el = D.querySelector(".js-plotly-plot");
      if (!el || !el._fullLayout) return;
      bindEvents(el);  // 圖表重建後重新綁定範圍事件
      // 擦拭中：重建後把橡皮擦按鈕重設為高亮
      if (W.__erasing) {
        const eb = D.querySelector(
          '.modebar-btn[data-title*="Erase"]'
        );
        if (eb) eb.classList.add("active");
      }
      if (saved === null || saved === undefined) {
        W.__savedDragmode = el._fullLayout.dragmode;  // 首次：記下目前工具
        return;
      }
      // eraseshape（橡皮擦）是暫時模式：期間不回復其他工具
      if (el._fullLayout.dragmode !== saved &&
          el._fullLayout.dragmode !== "eraseshape") {
        const btn = D.querySelector(
          '.modebar-btn[data-attr="dragmode"][data-val="' + saved + '"]'
        );
        if (btn) btn.click();  // 同時更新按鈕的啟用外觀
      }
    }, 300);

    // —— 隱形輸入框寫入通道（zoom_state／shapes_state）——
    // writeWidget：React 原生 setter 寫值 → fiber onChange 直呼
    // （繞過事件信任檢查）→ Streamlit 重跑、Python 套用。
    // 縮放：plotly 7 的過渡動畫（預設 500ms）期間 axis.range
    // 逐幀 tween，點擊當下直接讀會讀到中間值（「有時保存不到」
    // 的根因，來源已查證）；但 plotly_relayouting 在每個手勢
    // 步驟同步攜帶「目標範圍」，plotly_relayout 在動畫結束再
    // 確認一次——因此監聽兩事件、同步記下最新範圍（只記不寫），
    // 點擊按鈕的 capture 階段寫入、共用同一次 rerun。
    // 畫線：形狀編輯無動畫，現讀現寫（sanitize 後序列化），
    // 畫完 300ms 自動寫入持久化＋點擊時隨該互動同批提交。
    // 純縮放／畫線期間零寫入、零 rerun、零閃爍。
    const getSpec = () => W.__specJson || null;
    const writeWidget = (name, s) => {
      const wrappers = D.querySelectorAll('[data-testid="stTextInput"]');
      let input = null;
      for (const w of wrappers) {
        if (w.textContent.indexOf(name) !== -1) {
          input = w.querySelector("input");
          break;
        }
      }
      if (!input) return;
      const setter = Object.getOwnPropertyDescriptor(
        D.defaultView.HTMLInputElement.prototype, "value"
      ).set;
      // 寫入前記下目前捲動位置並凍結：focus／重建造成的捲動不算
      // （preventScroll 亦避免 focus 直接把頁面拉到底部）
      try { W.__savedScroll = D.defaultView.scrollY; } catch (err) {}
      W.__freezeScroll = Date.now() + 800;
      setter.call(input, s);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.focus({ preventScroll: true });
      input.blur();
      // 保險：沿 React fiber 樹向上找到 onChange 直接呼叫
      const fk = Object.keys(input).find((k) =>
        k.startsWith("__reactFiber$") ||
        k.startsWith("__reactInternalInstance$")
      );
      if (fk) {
        let fiber = input[fk];
        while (fiber) {
          const props = fiber.memoizedProps;
          if (props && typeof props.onChange === "function") {
            try { props.onChange({ target: input }); } catch (err) {}
            break;
          }
          fiber = fiber.return;
        }
      }
    };
    const onRange = (e) => {
      if (!e) return;
      const has = (k) => e[k] !== undefined && e[k] !== null;
      if (!(has("xaxis.range[0]") || has("xaxis.range[1]") ||
            has("yaxis.range[0]") || has("yaxis.range[1]") ||
            Array.isArray(e["xaxis.range"]) ||
            Array.isArray(e["yaxis.range"]))) return;
      const el = D.querySelector(".js-plotly-plot");
      const xa = el && el._fullLayout ? el._fullLayout.xaxis : null;
      const ya = el && el._fullLayout ? el._fullLayout.yaxis : null;
      if (!xa || !ya || !xa.range || !ya.range) return;
      const xr = Array.isArray(e["xaxis.range"]) ? e["xaxis.range"] : null;
      const yr = Array.isArray(e["yaxis.range"]) ? e["yaxis.range"] : null;
      const spec = getSpec();
      W.__pendingZoom = JSON.stringify({
        k: spec ? spec.dataKey : null,
        x0: has("xaxis.range[0]") ? e["xaxis.range[0]"]
          : (xr ? xr[0] : xa.range[0]),
        x1: has("xaxis.range[1]") ? e["xaxis.range[1]"]
          : (xr ? xr[1] : xa.range[1]),
        y0: has("yaxis.range[0]") ? e["yaxis.range[0]"]
          : (yr ? yr[0] : ya.range[0]),
        y1: has("yaxis.range[1]") ? e["yaxis.range[1]"]
          : (yr ? yr[1] : ya.range[1]),
      });
    };
    const sanitizeShapes = (shapes) => {
      const pick = (src, keys) => {
        const out = {};
        for (const k of keys) {
          if (src[k] !== undefined) out[k] = src[k];
        }
        return out;
      };
      const shapeKeys = ["type", "x0", "x1", "y0", "y1", "path",
                         "xref", "yref", "layer", "opacity",
                         "fillcolor", "editable", "name"];
      return shapes
        .filter((sh) => sh && sh.name !== "__price__")  // 現價線不持久化
        .map((sh) => {
        const c = pick(sh, shapeKeys);
        if (sh.line && typeof sh.line === "object") {
          c.line = pick(sh.line, ["color", "width", "dash"]);
        }
        return c;
      });
    };
    const currentZoom = () => {
      // 事件記錄的目標範圍優先；無記錄（剛重載）現讀兜底
      let z = W.__pendingZoom;
      if (!z) {
        const el = D.querySelector(".js-plotly-plot");
        if (el && el._fullLayout) {
          const xa = el._fullLayout.xaxis, ya = el._fullLayout.yaxis;
          if (xa && ya && xa.range && ya.range) {
            const spec = getSpec();
            z = JSON.stringify({
              k: spec ? spec.dataKey : null,
              x0: xa.range[0], x1: xa.range[1],
              y0: ya.range[0], y1: ya.range[1],
            });
          }
        }
      }
      return z;
    };
    const writeShapesAndZoom = (s) => {
      if (s !== W.__lastShapesWritten) {
        W.__lastShapesWritten = s;
        writeWidget("shapes_state", s);
        // 同批寫入現行縮放：rerun 後視圖不會跳回上次保存的範圍
        const z = currentZoom();
        if (z && z !== W.__lastZoomWritten) {
          W.__lastZoomWritten = z;
          writeWidget("zoom_state", z);
        }
      }
    };
    let _shapesTimer = null;
    const scheduleShapesWrite = () => {
      clearTimeout(_shapesTimer);
      _shapesTimer = setTimeout(() => {
        const el = D.querySelector(".js-plotly-plot");
        if (!el || !el._fullLayout ||
            !Array.isArray(el._fullLayout.shapes)) return;
        if (el._dragged) { scheduleShapesWrite(); return; }  // 拖曳中：稍後再寫
        const spec = getSpec();
        const s = JSON.stringify({
          k: spec ? spec.dataKey : null,
          shapes: sanitizeShapes(el._fullLayout.shapes),
        });
        writeShapesAndZoom(s);
      }, 300);
    };
    const onRelayout = (e) => {
      onRange(e);
      scheduleShapesWrite();  // 形狀可能已變：現讀現寫（值不變則不寫）
    };
    const bindEvents = (el) => {
      if (!el || el === W.__boundPlot) return;
      W.__boundPlot = el;
      el.on("plotly_relayouting", onRelayout);
      el.on("plotly_relayout", onRelayout);
    };
    // 自我診斷徽章：寫入結果直接顯示（雲端除錯用），2 秒後消失；
    // 位置往下錯開（top:64px），不擋右上角圖標；放在 app 容器外，
    // Streamlit rerun 不會清掉
    const setStatus = (msg, ok) => {
      let b = D.getElementById("kline-status");
      if (!b) {
        b = D.createElement("div");
        b.id = "kline-status";
        b.style.cssText = "position:fixed;top:64px;right:16px;" +
          "z-index:999999;padding:6px 12px;border-radius:8px;" +
          "font:12px/1.5 sans-serif;max-width:420px;" +
          "box-shadow:0 2px 8px rgba(0,0,0,.25);";
        (D.body || D.documentElement).appendChild(b);
      }
      W.__lastStatus = { msg: msg, ok: ok };  // 供 rerun 後的新實例補回
      b.textContent = msg;
      b.style.background = ok ? "#d9f2e6" : "#f8d7da";
      b.style.color = ok ? "#0b5c37" : "#a13030";
      b.style.border = "1px solid " + (ok ? "#2fa06b" : "#d9565c");
      clearTimeout(W.__statusTimer);
      W.__statusTimer = setTimeout(() => {
        if (b.parentNode) b.parentNode.removeChild(b);
      }, 2000);
    };
    const commitAll = () => {
      const el = D.querySelector(".js-plotly-plot");
      bindEvents(el);  // 圖表可能剛重建：先綁定再取範圍
      const spec = getSpec();
      // 縮放：事件記錄的目標範圍優先，無記錄現讀兜底
      const z = currentZoom();
      if (z && z !== W.__lastZoomWritten) {
        W.__lastZoomWritten = z;
        writeWidget("zoom_state", z);
        // 自我診斷徽章：寫入結果與失敗原因直接顯示（雲端除錯用）
        let why = "";
        try {
          const zj = JSON.parse(z);
          if (!spec || spec.dataKey !== zj.k) {
            why = "資料碼不符：寫入 k=" + zj.k +
                  " 目前=" + (spec ? spec.dataKey : "無");
          }
        } catch (err) { why = "JSON 異常"; }
        if (!why) {
          const n = D.querySelectorAll(
            '[data-testid="stTextInput"] input').length;
          if (n < 3) why = "隱形輸入框不足：" + n + "/3";
        }
        setStatus(why ? "縮放保存 ✗ " + why : "縮放保存 ✓ 已寫入", !why);
      } else if (!z) {
        const el2 = D.querySelector(".js-plotly-plot");
        const why2 = el2 ? (el2._fullLayout
          ? "圖表存在但範圍讀取失敗" : "圖表無 _fullLayout")
          : "找不到圖表元素";
        setStatus("縮放保存 ✗ 沒有可寫的縮放：" + why2, false);
      } else {
        setStatus("縮放無變化（與上次保存相同，未重寫）", true);
      }
      // 畫線：隨本次互動同批提交（與推進/交易共用同一次 rerun）
      if (el && el._fullLayout &&
          Array.isArray(el._fullLayout.shapes)) {
        const s = JSON.stringify({
          k: spec ? spec.dataKey : null,
          shapes: sanitizeShapes(el._fullLayout.shapes),
        });
        if (s !== W.__lastShapesWritten) {
          W.__lastShapesWritten = s;
          writeWidget("shapes_state", s);
        }
      }
    };

    // —— 縮放提交時機：只在「會觸發 rerun 的互動」前一刻寫入 ——
    // 每次寫入 zoom_state 都會觸發 Streamlit rerun＋圖表重建
    // （閃爍與卡頓的來源）。因此 pan/zoom 當下完全不寫；使用者
    // 點擊推進/交易按鈕、下拉選單選項、日期、勾選框等（這些互動
    // 本身就會 rerun）時，才在 capture 階段讀取圖表「現行」範圍
    // 寫入——與該互動的訊息同批送出、共用同一次 rerun，純縮放
    // 期間完全零寫入、零 rerun、零閃爍。
    // 每個新實例都註冊自己的提交勾（舊實例退位）：不綁死第一個
    // iframe 環境，也不綁死特定 testid——不同 Streamlit 版本的
    // DOM 差異都能相容（Community Cloud 會自行升級 Streamlit）
    const commitFromEvent = (e) => {
      if (W.__guardInstance !== myId) return;  // 舊實例：退位
      const t = e.target && e.target.closest ? e.target : null;
      if (!t || typeof t.closest !== "function") return;
      // 圖表內互動（含 modebar 工具列）不觸發 rerun，不提交
      if (t.closest(".js-plotly-plot")) return;
      // 點進文字輸入框準備打字：不提交，避免 rerun 搶走焦點
      if (t.closest('[data-testid="stTextInput"] input')) return;
      // 任何按鈕／選項類互動都「可能」觸發 rerun：提交待寫縮放
      const commitable = t.closest("button") ||
        t.closest('[role="button"]') || t.closest('[role="option"]') ||
        t.closest('[role="listbox"]') || t.closest('[role="checkbox"]') ||
        t.closest('[role="radio"]') || t.closest('[role="switch"]') ||
        t.closest('[data-baseweb="menu"]') ||
        t.closest('[data-baseweb="calendar"]');
      if (!commitable) return;
      commitAll();
    };
    // pointerdown 與 click 都會觸發（600ms 去重）：即使其中一種
    // 事件被攔截，另一種仍能送達提交
    const commitDeduped = (e) => {
      const now = Date.now();
      if (W.__lastCommitAt && now - W.__lastCommitAt < 600) return;
      W.__lastCommitAt = now;
      commitFromEvent(e);
    };
    onMulti(commitDeduped);
    W.addEventListener("pointerdown", commitDeduped, true);

    // —— 橡皮擦 ——
    // plotly 7 的 eraseshape 按鈕只刪「已啟用形狀」，而啟用機制
    // 被 config.edits.shapePosition 預設關閉，按鈕實際無效（來源
    // 已查證）。自行實作擦拭模式：點橡皮擦切換（按鈕高亮），
    // 再點線條即刪除（現價線 name="__price__" 除外）；點其他
    // 模式列工具或圖表外區域退出擦拭。
    onMulti((e) => {
      if (W.__guardInstance !== myId) return;  // 舊實例：退位
      const ne = Date.now();
      if (W.__lastEraseAt && ne - W.__lastEraseAt < 600) return;  // 多層註冊去重
      W.__lastEraseAt = ne;
      const t = e.target && e.target.closest ? e.target : null;
      if (!t || typeof t.closest !== "function") return;
        const mb = t.closest(".modebar-btn");
        if (mb) {
          const title = mb.getAttribute("data-title") ||
                        mb.getAttribute("title") || "";
          if (title.toLowerCase().indexOf("erase") !== -1) {
            W.__erasing = !W.__erasing;
            mb.classList.toggle("active", W.__erasing);
          } else {
            W.__erasing = false;  // 點其他工具：退出擦拭
          }
          return;
        }
        if (!W.__erasing) return;
        if (t.closest(".js-plotly-plot")) {
          const sh = t.closest(".shapelayer [data-index]");
          const el = D.querySelector(".js-plotly-plot");
          if (sh && el && el._fullLayout &&
              Array.isArray(el._fullLayout.shapes)) {
            const idx = +sh.getAttribute("data-index");
            const shapes = el._fullLayout.shapes;
            if (idx >= 0 && idx < shapes.length &&
                shapes[idx].name !== "__price__") {
              e.preventDefault();
              e.stopPropagation();
              const spec = getSpec();
              const s = JSON.stringify({
                k: spec ? spec.dataKey : null,
                shapes: sanitizeShapes(
                  shapes.filter((x, i) => i !== idx)),
              });
              writeShapesAndZoom(s);
            }
          }
        } else {
          W.__erasing = false;  // 點圖表外：退出擦拭
        }
    });

    // —— rerun 重建後回復捲動位置 ——
    const appRoot = D.querySelector("[data-testid='stAppViewContainer']");
    if (appRoot && !W.__observerHook) {
      W.__observerHook = true;
      new W.MutationObserver((mutations) => {
        const rebuilt = mutations.some(
          (m) => m.type === "childList" && m.removedNodes.length > 0
        );
        if (rebuilt) {
          setTimeout(() => D.defaultView.scrollTo(
            { top: W.__savedScroll, behavior: "instant" }
          ), 60);
        }
      }).observe(appRoot, { childList: true });
    }

    // —— B 站圖標：插在雲端外框 GitHub 圖標與 ⋮ 選單之間 ——
    // （登入版雲端頁面才有 GitHub 來源連結；本機無此元素會略過，
    // 由輪詢持續重試直到外框出現）
    const addBiliIcon = () => {
      try {
        // 工具列（GitHub 圖標／⋮）在應用文件的標頭（stHeader），
        // 標頭按鈕沒有 href／title 屬性（實測確認）——直接錨定
        // ⋮（stMainMenuButton）本身，把圖標插到它前面
        // （GitHub 圖標與 ⋮ 之間）
        const docs = [P.document, D];
        let anchor = null, host = null;
        for (const doc of docs) {
          if (doc.getElementById("kline-bili")) {
            W.__biliDone = true;
            return;
          }
          const menu = doc.querySelector(
            '[data-testid="stMainMenuButton"]') ||
            doc.querySelector('[data-testid="stMainMenu"]');
          if (menu) { anchor = menu; host = doc; break; }
        }
        if (!anchor) return;  // 標頭未出現：稍後再試
        W.__biliDone = true;
        const b = host.createElement("a");
        b.id = "kline-bili";
        b.href = "https://space.bilibili.com/1452749131";
        b.target = "_blank";
        b.rel = "noopener noreferrer";
        b.title = "B站空間";
        // SVG 以 DOM API 建構：st.html 內容若含 SVG 標記字串，
        // 前端消毒/解析會出問題（整段腳本不執行——已實測定位）
        const NS = "http://www.w3.org/2000/svg";
        const svg = host.createElementNS(NS, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("width", "18");
        svg.setAttribute("height", "18");
        const mk = (tag, attrs) => {
          const n = host.createElementNS(NS, tag);
          for (const k in attrs) n.setAttribute(k, attrs[k]);
          return n;
        };
        svg.appendChild(mk("path", { fill: "#fb7299",
          d: "M8.3 2.6 11 6.4l1 1.5 1-1.5 2.7-3.8c.5-.7 1.4-.9 2.1-.4" +
             ".7.5.9 1.4.4 2.1l-1.8 2.5h.1c2.7 0 4.9 2.2 4.9 4.9v5.4" +
             "c0 2.7-2.2 4.9-4.9 4.9H8.5c-2.7 0-4.9-2.2-4.9-4.9v-5.4" +
             "c0-2.7 2.2-4.9 4.9-4.9h.1L6.8 4.3c-.5-.7-.3-1.6.4-2.1" +
             ".7-.5 1.6-.4 2.1.4z" }));
        svg.appendChild(mk("circle", {
          fill: "#fff", cx: "9.8", cy: "14.6", r: "1.15" }));
        svg.appendChild(mk("circle", {
          fill: "#fff", cx: "15.2", cy: "14.6", r: "1.15" }));
        svg.appendChild(mk("path", { fill: "#fff",
          d: "M10.7 17.2h3.6c.8 0 1.3.9.8 1.5l-1.8 2.7c-.4.6-1.3.6" +
             "-1.7 0l-1.8-2.7c-.4-.6.1-1.5.9-1.5z" }));
        // 左側文字標籤：「关注小飯Aidan谢谢喵-->」指向小電視
        const lbl = host.createElement("span");
        lbl.textContent = "关注小飯Aidan谢谢喵-->";
        lbl.style.cssText = "font:12px/1 sans-serif;color:#fb7299;" +
          "white-space:nowrap;";
        b.appendChild(lbl);
        b.appendChild(svg);
        b.style.cssText = "display:inline-flex;align-items:center;" +
          "gap:4px;height:32px;padding:0 4px;margin:0 2px;" +
          "border-radius:8px;cursor:pointer;vertical-align:middle;";
        // 插到標頭最左（share 按鈕左邊）
        anchor.parentNode.insertBefore(b, anchor.parentNode.firstChild);
      } catch (err) {}
    };

    // 載入徽章：每次「整頁載入」顯示一次，證明新版已生效
    // （rerun 重建的新實例不重複顯示，避免蓋掉診斷訊息）
    addBiliIcon();
    if (!W.__loadBadgeShown) {
      W.__loadBadgeShown = true;
      setStatus("守護 v16 已啟動（縮放保存就緒）", true);
    } else if (W.__lastStatus) {
      // rerun 若清掉徽章，由新實例補回上一個狀態（雲端除錯用）
      setStatus(W.__lastStatus.msg, W.__lastStatus.ok);
    }
    })();
    </script>
    """,
    unsafe_allow_javascript=True,
)

# ---------- 參數設定 ----------
with st.sidebar:
    st.header("參數")
    market = st.selectbox("市場選擇", list(MARKETS), key="market")
    cur_code, cur_sym, cur_dec = MARKETS[market]
    st.caption(f"💱 貨幣單位：{cur_code}（{cur_sym}）")

    # 漲跌配色：兩地慣例可切換；空心漲／實心跌的形狀通道兩者皆保留
    candle_style = st.selectbox(
        "漲跌配色",
        ["紅漲綠跌（台/港/A股慣例）", "綠漲紅跌（美股慣例）"],
        key="candle_style",
    )

    ticker_hint = {
        "美股": "代號（如 AAPL）",
        "港股": "代號（如 700 或 0700）",
        "A股": "代號（如 600519 或 000001）",
    }[market]
    # 固定 key：標籤會隨市場變動，若無 key 切換市場時輸入值會被重置；
    # autocomplete="off"：避免瀏覽器輸出空 autocomplete 屬性（審計警告）
    ticker_input = st.text_input(ticker_hint, "AAPL",
                                 key="ticker_input",
                                 autocomplete="off").strip().upper()

    resolved, ticker_err = resolve_ticker(ticker_input, market)
    if ticker_err:
        st.error(ticker_err)
    elif resolved != ticker_input:
        st.caption(f"→ 自動補全為 {resolved}")

    # 預設近 3 個月：原地追加模式下蠟燭大小合理（區間越短蠟燭越大）
    start_date = st.date_input("開始日期", date.today() - timedelta(days=90))
    end_date = st.date_input("結束日期", date.today())

# 漲跌配色對應的 K 線顏色（買入/賣出按鈕背景、現價線與
# delta 徽章正負色也跟著這組配色走）
if candle_style.startswith("紅"):
    up_color, down_color = "#e34948", "#008300"
else:
    up_color, down_color = "#008300", "#e34948"
# 紅漲綠跌 → delta 正=紅、負=綠（inverse）；綠漲紅跌 → 正=綠、負=紅
delta_color_mode = "inverse" if candle_style.startswith("紅") else "normal"

# 買入=漲色底、賣出=跌色底：以按鈕 kind（primary/secondary）定位，
# 只作用於交易列（含股數輸入框的水平容器），不影響其他按鈕
st.markdown(f"""
<style>
[data-testid="stHorizontalBlock"]:has([data-testid="stNumberInput"])
    button[kind="primary"]:not(:disabled) {{
    background-color: {up_color}; border: none; color: #ffffff;
}}
[data-testid="stHorizontalBlock"]:has([data-testid="stNumberInput"])
    button[kind="secondary"]:not(:disabled) {{
    background-color: {down_color}; border: none; color: #ffffff;
}}
</style>
""", unsafe_allow_html=True)

if start_date >= end_date:
    st.error("開始日期必須早於結束日期")
    st.stop()
if ticker_err:
    st.stop()


# ---------- 資料載入 ----------
@st.cache_data(ttl=3600)  # 相同代號＋日期區間只下載一次
def load_data(ticker: str, start: str, end: str) -> pd.DataFrame:
    """下載歷史數據（yfinance 的 end 不含當天，呼叫端已 +1 天）。"""
    try:
        df = yf.download(ticker, start=start, end=end,
                         progress=False, auto_adjust=True)
    except Exception:
        return pd.DataFrame()  # 代號不存在或網路錯誤 → 空表

    if isinstance(df.columns, pd.MultiIndex):  # 新版 yfinance 可能是多層欄位
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])  # 去掉收盤價缺漏的列


def build_rangebreaks(data: pd.DataFrame):
    """產生 plotly rangebreaks：跳過週末與休市日，不佔圖表橫向空間。

    休市日由「資料區間內缺漏的工作日」自動推得，美股／港股／A股
    各自不同的國定假日都能正確跳過。
    """
    idx = data.index.tz_localize(None)
    missing = pd.date_range(idx.min(), idx.max(), freq="D").difference(idx)
    holidays = [d for d in missing if d.weekday() < 5]  # 週末由 bounds 處理
    return [dict(bounds=["sat", "mon"]),
            dict(values=[d.strftime("%Y-%m-%d") for d in holidays])]


with st.spinner(f"下載 {resolved} 歷史數據中…"):
    data = load_data(resolved, start_date.isoformat(),
                     (end_date + timedelta(days=1)).isoformat())

if data.empty:
    st.error(f"找不到 {resolved} 在 {start_date} ~ {end_date} 之間的資料，"
             f"請檢查代號或日期。")
    st.stop()

# ---------- 進度與模擬帳戶狀態 ----------
# 代號或日期改變時，重設進度與帳戶
data_key = f"{resolved}|{start_date}|{end_date}"
if st.session_state.get("data_key") != data_key:
    st.session_state.data_key = data_key
    st.session_state.idx = 0
    st.session_state.cash = INITIAL_CASH
    st.session_state.shares = 0
    st.session_state.avg_cost = 0.0
    st.session_state.trades = []  # {date, action, shares, price, pnl}
    st.session_state.wins = 0
    st.session_state.losses = 0
    st.session_state.realized_pnl = 0.0
    st.session_state.pop("prev_total", None)
    st.session_state.pop("prev_ret", None)
st.session_state.total = len(data)  # 供按鈕 callback 使用

idx = min(st.session_state.get("idx", 0), len(data) - 1)  # 防呆：不超出範圍
is_last = idx == len(data) - 1

# 當天資料與貨幣（顯示與交易 callback 共用；callback 在 rerun 前執行，
# 讀到的是前一次執行寫入、也就是目前這一天的值）
current_date = data.index[idx]
current = data.iloc[idx]
close = float(current["Close"])
st.session_state.current_date = current_date
st.session_state.current_close = close
st.session_state.cur_sym = cur_sym
st.session_state.cur_dec = cur_dec


def money(v: float) -> str:
    """依市場貨幣格式顯示金額。"""
    return f"{cur_sym}{v:,.{cur_dec}f}"


def money_delta(v: float) -> str:
    """帶正負號的金額變化。

    正負號必須在字串開頭：st.metric 只檢查第一個字元判斷升跌方向，
    若寫成「$+1.23」會一律被當成上升（下跌也會塗成漲色徽章）。
    """
    return f"{'+' if v >= 0 else '-'}{cur_sym}{abs(v):,.{cur_dec}f}"


# ---------- 顯示當天資訊 ----------
prev_close = data.iloc[idx - 1]["Close"] if idx > 0 else None

col1, col2, col3 = st.columns(3)
col1.metric("📅 目前日期", current_date.strftime("%Y-%m-%d"))
close_chg = close - prev_close if prev_close is not None else 0.0
col2.metric(
    "💰 收盤價",
    money(close),
    delta=(money_delta(close_chg) if abs(close_chg) > 1e-9 else None),
    delta_color=delta_color_mode,  # 正負色跟隨漲跌配色；0 時不顯示徽章
)
col3.metric("📊 進度", f"{idx + 1} / {len(data)} 天")

# ---------- 資產看板 ----------
cash = st.session_state.cash
shares = st.session_state.shares
avg_cost = st.session_state.avg_cost
total_assets = cash + shares * close
ret_pct = (total_assets - INITIAL_CASH) / INITIAL_CASH * 100
prev_total = st.session_state.get("prev_total", total_assets)
prev_ret = st.session_state.get("prev_ret", ret_pct)
st.session_state.prev_total = total_assets
st.session_state.prev_ret = ret_pct

st.subheader(f"💼 資產看板（{cur_code}）")
# 金額欄位（現金／總資產）拉寬，避免長數字被截斷
m1, m2, m3, m4, m5, m6 = st.columns([0.8, 1.0, 1.25, 1.25, 1.0, 1.0])
m1.metric("持倉數量", f"{shares} 股")
m2.metric("平均成本", money(avg_cost) if shares else "—")
m3.metric("剩餘現金", money(cash))
total_chg = total_assets - prev_total
m4.metric("總資產", money(total_assets),
          delta=(money_delta(total_chg) if abs(total_chg) > 1e-9 else None),
          delta_color=delta_color_mode)
m5.metric("總收益", money_delta(total_assets - INITIAL_CASH))
# 累積收益率的 delta＝這次時間前進帶來的變化；正負色跟隨漲跌配色
ret_chg = ret_pct - prev_ret
m6.metric("累積收益率", f"{ret_pct:+.2f}%",
          delta=(f"{ret_chg:+.2f}%" if abs(ret_chg) > 1e-9 else None),
          delta_color=delta_color_mode)

# ---------- 交易操作 ----------
st.subheader("🛒 交易操作")


def buy():
    """買入：移動平均成本法計入均價。"""
    n = int(st.session_state.shares_input)
    price = st.session_state.current_close
    sym = st.session_state.get("cur_sym", "$")
    dec = st.session_state.get("cur_dec", 2)
    cost = n * price
    if cost > st.session_state.cash + 1e-9:
        st.session_state.trade_msg = (
            "warning", f"現金不足：需要 {sym}{cost:,.{dec}f}，"
                       f"僅剩 {sym}{st.session_state.cash:,.{dec}f}")
        return
    total_shares = st.session_state.shares + n
    st.session_state.avg_cost = (
        st.session_state.shares * st.session_state.avg_cost + cost
    ) / total_shares
    st.session_state.shares = total_shares
    st.session_state.cash -= cost
    st.session_state.trades.append({
        "date": st.session_state.current_date, "action": "買入",
        "shares": n, "price": price, "pnl": 0.0,
    })
    st.session_state.trade_msg = (
        "success", f"買入 {n} 股 @ {sym}{price:,.{dec}f}，"
                   f"花費 {sym}{cost:,.{dec}f}")


def sell():
    """賣出：以平均成本法結算已實現損益與勝負。"""
    n = int(st.session_state.shares_input)
    if n > st.session_state.shares:
        st.session_state.trade_msg = (
            "warning", f"持倉不足：目前僅持有 {st.session_state.shares} 股")
        return
    price = st.session_state.current_close
    sym = st.session_state.get("cur_sym", "$")
    dec = st.session_state.get("cur_dec", 2)
    proceeds = n * price
    pnl = (price - st.session_state.avg_cost) * n
    st.session_state.cash += proceeds
    st.session_state.shares -= n
    st.session_state.realized_pnl += pnl
    if pnl > 0:
        st.session_state.wins += 1
    elif pnl < 0:
        st.session_state.losses += 1
    st.session_state.trades.append({
        "date": st.session_state.current_date, "action": "賣出",
        "shares": n, "price": price, "pnl": pnl,
    })
    if st.session_state.shares == 0:
        st.session_state.avg_cost = 0.0
    st.session_state.trade_msg = (
        "success", f"賣出 {n} 股 @ {sym}{price:,.{dec}f}，"
                   f"損益 {sym}{pnl:+,.{dec}f}")


# 股數＋買＋賣同一行緊湊排列（bottom 對齊讓按鈕與輸入框底部切齊）
col_in, col_b1, col_b2 = st.columns([0.8, 1, 1],
                                    vertical_alignment="bottom")
col_in.number_input("股數", min_value=1, step=1, value=10,
                    key="shares_input", disabled=is_last)
# 買入=primary（漲色底）、賣出=secondary（跌色底），顏色由 CSS 控制
col_b1.button("買入", type="primary", on_click=buy, disabled=is_last)
col_b2.button("賣出", on_click=sell, disabled=is_last)

# 交易結果訊息（callback 寫入，本次 rerun 顯示一次）
msg = st.session_state.pop("trade_msg", None)
if msg:
    kind, text = msg
    (st.warning if kind == "warning" else st.success)(text)
if is_last:
    st.info("已到結束日期，模擬結算完成，交易功能停用。")

# ---------- 圖表：K 線 + 均線 + 買賣點 ----------
# 均線是回顧型指標，先在完整資料上計算再切片到「今天」，
# 數值不變，但能保證未來數據絕不會出現在圖上。
# 配色經調色盤驗證器檢驗：相鄰對正常視覺 ΔE ≥ 24、CVD ΔE ≥ 9.2；
# 三個淺色系靠線尾標籤＋圖例輔助辨識（驗證器的 relief 要求）。
MAS = [
    (5, "#eb6834"),    # 橘
    (10, "#1baf7a"),   # 藍綠
    (20, "#2a78d6"),   # 藍
    (60, "#e87ba4"),   # 洋紅
    (120, "#4a3aa7"),  # 紫
    (250, "#eda100"),  # 琥珀
]
mas = {w: data["Close"].rolling(w).mean() for w, _ in MAS}
visible = data.iloc[: idx + 1]  # 只顯示到當前模擬日期

# X 與 Y 預設固定為完整資料區間：點擊推進時既有 K 線的位置與
# 大小完全不變，新蠟燭在屬於它的位置上原地出現（右側未到之處留白）。
x_range = [data.index[0], data.index[-1]]
# Y 固定為全區間高低點（含 1% 餘裕給買賣點標註）
y_range = [data["Low"].min() * 0.99, data["High"].max() * 1.01]
# 縮放視圖：前端守護監聽 plotly 縮放事件、同步記下「目標範圍」，
# 在「會觸發 rerun 的互動」（推進/交易按鈕等）前一刻寫入隱形
# zoom_state 輸入框（React fiber onChange 通道），與該互動共用
# 同一次 rerun——純縮放期間零寫入、不重跑、不閃爍；
# 資料識別碼不符（換標的/日期區間）時忽略、回到預設全範圍
_zoom = st.session_state.get("zoom_state", "")
if _zoom:
    try:
        _z = json.loads(_zoom)
        if _z.get("k") == data_key:
            x_range = [pd.to_datetime(_z["x0"], unit="ms"),
                       pd.to_datetime(_z["x1"], unit="ms")]
            y_range = [float(_z["y0"]), float(_z["y1"])]
    except (ValueError, TypeError, KeyError):
        pass

# 畫線（使用者畫的支撐/壓力/趨勢線等 plotly shapes）：
# 前端守護畫完 300ms 或點擊互動前一刻現讀現寫入 shapes_state，
# 這裡讀取並套用（形狀座標為資料座標，推進時自然跟著圖走）；
# 資料識別碼不符（換標的/日期區間）時忽略、新圖從空白開始
user_shapes = []
_shapes_raw = st.session_state.get("shapes_state", "")
if _shapes_raw:
    try:
        _sh = json.loads(_shapes_raw)
        if _sh.get("k") == data_key and isinstance(_sh.get("shapes"), list):
            user_shapes = [sh for sh in _sh["shapes"] if isinstance(sh, dict)]
    except (ValueError, TypeError):
        pass
# 畫線保持可編輯（頂點拖曳微調），rerun 後仍可繼續調整
for _shp in user_shapes:
    _shp.setdefault("editable", True)

# 圖表配色固定為淺色白底（與應用主題無關，統一白色）
surface, paper, ink, ink2, grid = (
    "#ffffff", "#ffffff", "#0b0b0b", "#52514e", "#e1e0d9")

# 漲=空心（白底）、跌=實心——空心／實心的形狀差異同時是
# 色弱讀者的第二個辨識通道，兩種配色模式皆保留。
# 邊框 width=1：空心蠟燭的描邊維持細線
fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=visible.index,
    open=visible["Open"], high=visible["High"],
    low=visible["Low"], close=visible["Close"],
    name="K線", showlegend=False,  # 蠟燭自明；圖例只留均線與買賣點
    increasing=dict(line=dict(color=up_color, width=1),
                    fillcolor=surface),
    decreasing=dict(line=dict(color=down_color, width=1),
                    fillcolor=down_color),
))
for w, color in MAS:
    fig.add_trace(go.Scatter(
        x=visible.index, y=mas[w].iloc[: idx + 1],
        mode="lines", name=f"MA{w}",
        line=dict(color=color, width=2),
    ))

# 買賣點標註：買=綠色▲在 K 線下方、賣=紅色▼在上方。
# 顏色＋形狀＋位置三重編碼，避免與紅漲綠跌的蠟燭混淆。
buys = [t for t in st.session_state.trades if t["action"] == "買入"]
sells = [t for t in st.session_state.trades if t["action"] == "賣出"]
if buys:
    fig.add_trace(go.Scatter(
        x=[t["date"] for t in buys],
        y=[data.loc[t["date"], "Low"] * 0.995 for t in buys],
        mode="markers", name="買入",
        marker=dict(symbol="triangle-up", size=13, color="#0ca30c",
                    line=dict(width=1.5, color="#ffffff")),
        customdata=[[t["shares"], t["price"]] for t in buys],
        hovertemplate=(f"買入 %{{customdata[0]}} 股 @ {cur_sym}"
                       f"%{{customdata[1]:.{cur_dec}f}}<extra></extra>"),
    ))
if sells:
    fig.add_trace(go.Scatter(
        x=[t["date"] for t in sells],
        y=[data.loc[t["date"], "High"] * 1.005 for t in sells],
        mode="markers", name="賣出",
        marker=dict(symbol="triangle-down", size=13, color="#d03b3b",
                    line=dict(width=1.5, color="#ffffff")),
        customdata=[[t["shares"], t["price"]] for t in sells],
        hovertemplate=(f"賣出 %{{customdata[0]}} 股 @ {cur_sym}"
                       f"%{{customdata[1]:.{cur_dec}f}}<extra></extra>"),
    ))

# 線尾直接標註（標籤用中性墨色，不用系列色）。
# 目前 K 線固定在右緣，標籤一律放在線尾左側，避免被右緣裁切。
for w, _ in MAS:
    s = mas[w].iloc[: idx + 1]
    if s.notna().any():
        x_end = s.last_valid_index()
        fig.add_annotation(
            x=x_end, y=s[x_end], text=f"MA{w}", showarrow=False,
            xanchor="right", xshift=-6,
            font=dict(color=ink2, size=10),
        )

# 當前股價線：顏色跟隨當日漲跌（與 K 線同色系）；
# 價籤以 yshift 與虛線錯開（接近頂部時改放線下方）
price_color = (up_color if prev_close is None or close >= prev_close
               else down_color)
y_span = visible["High"].max() - visible["Low"].min()
tag_shift = -14 if close > visible["Low"].min() + 0.85 * y_span else 10
# 現價線以「有名稱的形狀」加入（name="__price__"）：前端以此辨識
# ——不納入畫線持久化、擦拭時也不可刪除
price_shape = dict(type="line", xref="paper", x0=0, x1=1,
                   yref="y", y0=close, y1=close,
                   line=dict(color=price_color, width=1, dash="dot"),
                   name="__price__")
fig.add_annotation(
    xref="paper", yref="y", x=0, xanchor="left", y=close,
    yshift=tag_shift,
    text=f"現價 {money(close)}", showarrow=False,
    font=dict(color=price_color, size=11),
)

fig.update_layout(
    # 資料不變（僅推進天數）時保留使用者縮放／平移狀態；
    # 換代號或日期時 uirevision 跟著變，縮放狀態合理重置
    uirevision=data_key,
    # 標題靠左；圖例放圖表下方並預留固定空間（b=70 可容兩行），
    # 交易後圖例項目增減也不會壓縮繪圖區、改變線圖大小
    title=dict(text=f"{resolved} 日線（資料截至 {current_date:%Y-%m-%d}）",
               x=0.01, xanchor="left"),
    # 關掉 plotly 過渡動畫（layout.transition 預設 500ms）：動畫
    # 期間 axis.range 逐幀 tween，前端點擊當下讀範圍會讀到中間值；
    # duration=0 讓縮放平移直接到位（訓練工具要精確，不需動畫）
    transition=dict(duration=0),
    # 預設工具為平移（框選縮放 zoom2d 已從模式列移除；
    # 縮放用滾輪或放大/縮小按鈕）
    dragmode="pan",
    # 新畫線樣式（墨色寬 2、矩形全透明——只留框線不遮罩，
    # 蠟燭顏色不會被繪製區塊改變）
    newshape=dict(line=dict(color=ink, width=2),
                  fillcolor="rgba(0,0,0,0)"),
    xaxis=dict(gridcolor=grid,
               rangebreaks=build_rangebreaks(data),  # 跳過休市日
               # X 固定為完整區間：推進時既有 K 線位置與圖表寬度不變
               range=x_range,
               automargin=False),
    xaxis_rangeslider_visible=False,  # 隱藏範圍滑桿，避免偷看區間外
    hovermode="x unified",  # 統一的十字懸停提示
    plot_bgcolor=surface,
    paper_bgcolor=paper,
    font=dict(color=ink, size=12),
    # Y 固定為完整區間＋關閉自動邊距：推進時蠟燭高度也不變
    yaxis=dict(gridcolor=grid, automargin=False, range=y_range),
    legend=dict(orientation="h", yanchor="top", y=-0.06,
                x=0.5, xanchor="center", font=dict(size=11),
                itemsizing="constant"),
    margin=dict(t=80, b=70, l=70, r=15),
)

# 形狀以「完整替換」套用：update_layout 的陣列屬性是索引合併
# （刪線後殘留元素不會消失、反而重複），因此直接指派整個列表——
# 現價線在前（name="__price__"）、使用者畫線在後，每次 rerun
# 都從零重建，擦拭刪除才真正生效
fig.layout.shapes = [price_shape] + user_shapes

# 固定 key＋固定高度：組件身份穩定、高度不因 rerun 閃動；
# on_select="ignore" 讓框選等選擇狀態不觸發 rerun、也不被重置
# 把資料識別碼交給前端守護（供縮放視圖的 key 校驗）
_spec_json = json.dumps({"dataKey": data_key})
st.html(
    f"""<script>
    (function () {{
    parent.window.__specJson = {_spec_json};
    }})();
    </script>""",
    unsafe_allow_javascript=True,
)

st.plotly_chart(fig, key="kline_chart", on_select="ignore",
                config={"scrollZoom": True,
                        # 明確列出模式列按鈕（取代預設）：Streamlit 前端
                        # 會把自己的全螢幕按鈕前插進 modeBarButtonsToAdd，
                        # 與巢狀群組混合會弄亂模式列（放大縮小按鈕消失）；
                        # 明確列法經 plotly 按鈕註冊表解析，工具保證齊全
                        "modeBarButtons": [
                            ["pan2d", "zoomIn2d", "zoomOut2d",
                             "autoScale2d", "resetScale2d"],
                            ["drawline", "drawrect", "drawopenpath",
                             "eraseshape"],
                            ["toImage"],
                        ]},
                height=620)

# ---------- 結束自動結算 ----------
if is_last:
    closed = st.session_state.wins + st.session_state.losses
    win_rate = st.session_state.wins / closed * 100 if closed else 0.0
    st.success(
        f"🎉 模擬結束（{current_date:%Y-%m-%d}）\n\n"
        f"- 最終總資產：**{money(total_assets)}**"
        f"（初始 {money(INITIAL_CASH)}）\n"
        f"- 最終收益率：**{ret_pct:+.2f}%**\n"
        f"- 總交易次數：**{len(st.session_state.trades)}** 筆\n"
        f"- 已實現損益：{money_delta(st.session_state.realized_pnl)}\n"
        f"- 勝率：**{win_rate:.1f}%**"
        f"（{st.session_state.wins} 勝 / {st.session_state.losses} 負 / "
        f"已平倉 {closed} 筆）"
    )

# ---------- 按鈕控制 ----------
# 用 on_click callback 在 rerun「之前」更新狀態，
# 這樣上方顯示區塊當次就能讀到新的一天（直接寫在按鈕區塊會慢一拍）。
def advance(days: int):
    """推進 N 個交易日（不足則到最後一天）。"""
    st.session_state.idx = min(st.session_state.idx + days,
                               st.session_state.total - 1)


def next_day():
    advance(1)


def reset_sim():
    """重設整個模擬：回到第一天並清空帳戶。"""
    st.session_state.idx = 0
    st.session_state.cash = INITIAL_CASH
    st.session_state.shares = 0
    st.session_state.avg_cost = 0.0
    st.session_state.trades = []
    st.session_state.wins = 0
    st.session_state.losses = 0
    st.session_state.realized_pnl = 0.0
    st.session_state.pop("prev_total", None)
    st.session_state.pop("prev_ret", None)


col_btn1, col_btn2, col_btn3, col_btn4 = st.columns([1, 1, 1, 1])
clicked_next = col_btn1.button("下一天 ➡️", on_click=next_day)
clicked_week = col_btn2.button("下一週 ⏩（+5天）",
                               on_click=lambda: advance(5))
clicked_month = col_btn3.button("下一月 ⏭️（+20天）",
                                on_click=lambda: advance(20))
col_btn4.button("重設 🔄", on_click=reset_sim)

if (clicked_next or clicked_week or clicked_month) and is_last:
    st.warning("已到資料最後一天！")

# 縮放保存與畫線的通訊管道：zoom_state／shapes_state 輸入框以
# CSS 完全隱藏（testid 定位），不佔任何可見空間。前端守護把
# 縮放範圍（互動前一刻）與畫線形狀（畫完 300ms 或隨互動）寫
# 進來，Python 端讀取後直接套用到圖表規格。
st.markdown(
    """
    <style>
    div:has(> [data-testid="stMarkdownContainer"] .zoom-anchor) {
        display: none !important;
    }
    div:has(> [data-testid="stMarkdownContainer"] .zoom-anchor) + div {
        display: none !important;
    }
    div:has(> [data-testid="stMarkdownContainer"] .zoom-anchor) + div + div {
        display: none !important;
    }
    </style>
    <div class="zoom-anchor"></div>
    """,
    unsafe_allow_html=True,
)
st.text_input("zoom_state", key="zoom_state",
              label_visibility="collapsed", autocomplete="off")
st.text_input("shapes_state", key="shapes_state",
              label_visibility="collapsed", autocomplete="off")
