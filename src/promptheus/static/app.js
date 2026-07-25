// First-party behaviour htmx cannot express. Plan 07 chose to have none of
// this; plan 08 reverses that for the comparison features in plan 09.
(function () {
  "use strict";

  function columnOf(element) {
    return element.closest("[data-column]");
  }

  function answerText(column) {
    const answer = column.querySelector(".answer");
    return answer ? answer.innerText.trim() : "";
  }

  document.addEventListener("click", function (event) {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const column = columnOf(copy);
      if (!column) return;
      navigator.clipboard.writeText(answerText(column)).then(
        function () {
          const previous = copy.textContent;
          copy.textContent = "copied";
          setTimeout(function () {
            copy.textContent = previous;
          }, 1200);
        },
        function () {
          copy.textContent = "copy failed";
        },
      );
      return;
    }

    const toggle = event.target.closest("[data-collapse]");
    if (toggle) {
      const column = columnOf(toggle);
      if (!column) return;
      const collapsed = column.toggleAttribute("data-collapsed");
      toggle.textContent = collapsed ? "expand" : "collapse";
    }
  });

  // Aggregate progress. The stream names each terminal event "<slug>-done",
  // so counting those is enough — no extra endpoint, no server state.
  document.addEventListener("htmx:sseMessage", function (event) {
    const results = event.target.closest("[data-results]");
    if (!results || !event.detail || !event.detail.type) return;
    if (!event.detail.type.endsWith("-done")) return;

    const progress = results.querySelector("[data-progress]");
    if (!progress) return;

    const done = Number(progress.dataset.done || 0) + 1;
    const total = Number(progress.dataset.total || 0);
    progress.dataset.done = String(done);
    progress.textContent = done + " of " + total + " done";
  });
})();
