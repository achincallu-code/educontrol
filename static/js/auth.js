document.addEventListener("DOMContentLoaded", () => {
  const shell = document.querySelector("[data-auth-shell]");
  if (!(shell instanceof HTMLElement)) return;

  const formStage = shell.querySelector("[data-auth-form-stage]");
  const panels = Array.from(shell.querySelectorAll("[data-auth-panel]"));
  const copies = Array.from(shell.querySelectorAll("[data-auth-copy]"));
  const mobileLayout = window.matchMedia("(max-width: 760px)");
  let stageResizeTimer;

  const updateStageHeight = (fromHeight) => {
    if (!mobileLayout.matches || !(formStage instanceof HTMLElement)) {
      if (formStage instanceof HTMLElement) formStage.style.height = "";
      return;
    }

    const activePanel = shell.querySelector("[data-auth-panel].is-active");
    if (!(activePanel instanceof HTMLElement)) return;
    if (typeof fromHeight === "number") formStage.style.height = `${fromHeight}px`;

    window.requestAnimationFrame(() => {
      if (!activePanel.isConnected) return;
      formStage.style.height = `${activePanel.scrollHeight}px`;
      window.clearTimeout(stageResizeTimer);
      stageResizeTimer = window.setTimeout(() => {
        formStage.style.height = "";
      }, 650);
    });
  };

  const switchMode = (mode, { updateHistory = false, focus = false } = {}) => {
    const nextPanel = panels.find((panel) => panel.dataset.authPanel === mode);
    if (!(nextPanel instanceof HTMLElement)) return;

    const currentPanel = panels.find((panel) => panel.classList.contains("is-active"));
    if (currentPanel === nextPanel) return;

    const currentHeight = mobileLayout.matches && formStage instanceof HTMLElement
      ? formStage.getBoundingClientRect().height
      : undefined;

    panels.forEach((panel) => {
      const isActive = panel === nextPanel;
      panel.classList.toggle("is-active", isActive);
      panel.setAttribute("aria-hidden", String(!isActive));
      if (isActive) {
        panel.removeAttribute("inert");
      } else {
        panel.setAttribute("inert", "");
      }
    });

    copies.forEach((copy) => {
      const isActive = copy.dataset.authCopy === mode;
      copy.setAttribute("aria-hidden", String(!isActive));
    });
    shell.querySelectorAll(".auth-top-switch-link").forEach((link) => {
      const isActive = link instanceof HTMLAnchorElement && link.dataset.authSwitch !== mode;
      link.setAttribute("aria-hidden", String(!isActive));
      if (isActive) {
        link.removeAttribute("inert");
      } else {
        link.setAttribute("inert", "");
      }
    });
    shell.classList.toggle("is-register", mode === "register");
    shell.dataset.authMode = mode;

    if (updateHistory) {
      const switchLink = shell.querySelector(`[data-auth-switch="${mode}"]`);
      if (switchLink instanceof HTMLAnchorElement) {
        window.history.pushState({ authMode: mode }, "", switchLink.href);
      }
    }

    document.title = mode === "register"
      ? "Registro · EduControl"
      : "Acceso · EduControl";
    updateStageHeight(currentHeight);

    if (focus) {
      nextPanel.querySelector("h2")?.focus({ preventScroll: true });
    }
  };

  shell.querySelectorAll("[data-auth-switch]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!(link instanceof HTMLAnchorElement)) return;
      const mode = link.dataset.authSwitch;
      if (mode !== "login" && mode !== "register") return;
      event.preventDefault();
      switchMode(mode, { updateHistory: true, focus: true });
    });
  });

  window.addEventListener("popstate", () => {
    const mode = window.location.pathname.endsWith("/registro") ? "register" : "login";
    switchMode(mode);
  });

  mobileLayout.addEventListener("change", () => updateStageHeight());
  updateStageHeight();

  const passwordInput = shell.querySelector("[data-password-meter-input]");
  const passwordMeter = shell.querySelector("[data-password-meter]");
  const passwordMeterLabel = shell.querySelector("[data-password-meter-label]");

  if (passwordInput instanceof HTMLInputElement
    && passwordMeter instanceof HTMLElement
    && passwordMeterLabel instanceof HTMLElement) {
    passwordInput.addEventListener("input", () => {
      const value = passwordInput.value;
      const characterGroups = [
        /[a-z]/.test(value),
        /[A-Z]/.test(value),
        /\d/.test(value),
        /[^A-Za-z0-9]/.test(value)
      ].filter(Boolean).length;

      let strength = 0;
      let label = "Usa al menos 8 caracteres";
      if (value.length >= 8) {
        strength = 1;
        label = "Inicial";
      }
      if (value.length >= 10 && characterGroups >= 2) {
        strength = 2;
        label = "En desarrollo";
      }
      if (value.length >= 12 && characterGroups >= 3) {
        strength = 3;
        label = "Buena";
      }
      if (value.length >= 16 && characterGroups >= 3) {
        strength = 4;
        label = "Sólida";
      }
      if (value.length > 128) label = "Máximo 128 caracteres";

      passwordMeter.dataset.strength = String(strength);
      passwordMeterLabel.textContent = label;
    });
  }

  shell.querySelectorAll("[data-auth-submit]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!(form instanceof HTMLFormElement) || !form.checkValidity()) return;
      const submitButton = form.querySelector('button[type="submit"]');
      if (!(submitButton instanceof HTMLButtonElement)) {
        event.preventDefault();
        return;
      }
      submitButton.disabled = true;
      submitButton.setAttribute("aria-busy", "true");
    });
  });

  window.addEventListener("pageshow", () => {
    shell.querySelectorAll('button[type="submit"][aria-busy="true"]').forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) return;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    });
  });
});
