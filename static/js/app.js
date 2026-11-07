document.addEventListener("DOMContentLoaded", () => {
  const themeColors = {
    green: "#087443",
    blue: "#1767a6",
    violet: "#7045a2",
    rose: "#b04469",
    amber: "#ad5a1b"
  };
  const root = document.documentElement;
  const systemAppearance = window.matchMedia("(prefers-color-scheme: dark)");
  const updateAppearance = () => {
    const isDark = root.dataset.appearance === "dark"
      || (root.dataset.appearance === "system" && systemAppearance.matches);
    const color = isDark
      ? "#121a16"
      : (themeColors[root.dataset.colorTheme] || themeColors.green);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", color);
  };
  updateAppearance();
  systemAppearance.addEventListener("change", updateAppearance);

  document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
    const input = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!(input instanceof HTMLInputElement)) return;
    toggle.addEventListener("click", () => {
      const showPassword = input.type === "password";
      input.type = showPassword ? "text" : "password";
      toggle.textContent = showPassword ? "Ocultar" : "Mostrar";
      toggle.setAttribute("aria-label", showPassword ? "Ocultar contraseña" : "Mostrar contraseña");
    });
  });

  document.querySelectorAll("[data-confirm-message]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirmMessage || "")) {
        event.preventDefault();
      }
    });
  });

  const quickStudentSearch = document.querySelector("[data-quick-student-search]");
  const quickGroup = quickStudentSearch?.querySelector("[data-quick-group]");
  const quickListNumber = quickStudentSearch?.querySelector("[data-quick-list-number]");
  let quickSearchTimer;
  const submitQuickStudentSearch = () => {
    if (quickGroup?.value && quickListNumber?.value && quickListNumber.checkValidity()) {
      quickStudentSearch.requestSubmit();
    }
  };
  quickGroup?.addEventListener("change", submitQuickStudentSearch);
  quickListNumber?.addEventListener("input", () => {
    window.clearTimeout(quickSearchTimer);
    if (quickGroup?.value && quickListNumber.value) {
      quickSearchTimer = window.setTimeout(submitQuickStudentSearch, 350);
    }
  });

  const studentDirectorySearch = document.querySelector("[data-student-directory-search]");
  const studentSearchInput = studentDirectorySearch?.querySelector('input[name="q"]');
  let studentSearchTimer;
  studentSearchInput?.addEventListener("input", () => {
    window.clearTimeout(studentSearchTimer);
    studentSearchTimer = window.setTimeout(() => {
      studentDirectorySearch.requestSubmit();
    }, 350);
  });
  studentDirectorySearch?.querySelectorAll("select").forEach((select) => {
    select.addEventListener("change", () => studentDirectorySearch.requestSubmit());
  });

  document.querySelectorAll(".activity-filter-bar select").forEach((select) => {
    select.addEventListener("change", () => select.form?.requestSubmit());
  });

  const favoriteOptions = document.querySelector("[data-favorite-options]");
  const favoriteLimitMessage = document.querySelector("[data-favorite-limit-message]");
  favoriteOptions?.addEventListener("change", (event) => {
    if (!(event.target instanceof HTMLInputElement)) return;
    if (!event.target.checked) {
      if (favoriteLimitMessage) favoriteLimitMessage.textContent = "";
      return;
    }
    const checkedFavorites = favoriteOptions.querySelectorAll("input:checked");
    if (checkedFavorites.length > 3) {
      event.target.checked = false;
      if (favoriteLimitMessage) {
        favoriteLimitMessage.textContent = "Puedes elegir hasta tres accesos.";
      }
      return;
    }
    if (favoriteLimitMessage) favoriteLimitMessage.textContent = "";
  });

  const sidebar = document.getElementById("sidebar");
  const menuToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarScrim = document.querySelector("[data-sidebar-close]");

  const closeSidebar = () => {
    sidebar?.classList.remove("open");
    document.body.classList.remove("sidebar-open");
    menuToggle?.setAttribute("aria-expanded", "false");
  };

  menuToggle?.addEventListener("click", () => {
    const isOpen = sidebar?.classList.toggle("open") ?? false;
    document.body.classList.toggle("sidebar-open", isOpen);
    menuToggle.setAttribute("aria-expanded", String(isOpen));
  });
  sidebarScrim?.addEventListener("click", closeSidebar);
  sidebar?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", closeSidebar);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });

  const assistant = document.querySelector("[data-cecyto-assistant]");
  const mascotMessage = document.querySelector("[data-mascot-message]");
  if (assistant && mascotMessage) {
    if (assistant.hasAttribute("data-student-assistant")) {
      const motivations = JSON.parse(assistant.dataset.motivations || "[]");
      if (assistant.dataset.welcome === "true") {
        mascotMessage.textContent = "¡Qué gusto verte! Cecyto te acompaña.";
      } else if (motivations.length) {
        const index = Math.floor(Math.random() * motivations.length);
        mascotMessage.textContent = motivations[index];
      }
    }
  }

  const celebrationDialog = document.querySelector("[data-celebration-dialog]");
  if (celebrationDialog) {
    const closeButtons = celebrationDialog.querySelectorAll("[data-celebration-close]");
    const celebrationTitle = celebrationDialog.querySelector("#celebration-title");
    const celebrationDescription = celebrationDialog.querySelector("#celebration-description");
    const encouragements = [
      "¡Excelente trabajo!",
      "¡Felicidades, lo lograste!",
      "¡Cada esfuerzo cuenta!",
      "¡Vas por muy buen camino!",
      "¡Sigue así, futuro profesional!"
    ];
    const closeCelebration = () => {
      celebrationDialog.hidden = true;
      document.body.classList.remove("celebration-open");
      const ids = JSON.parse(celebrationDialog.dataset.celebrationIds || "[]");
      if (ids.length) {
        const body = new URLSearchParams({
          csrf_token: celebrationDialog.dataset.csrfToken || "",
          ids: JSON.stringify(ids)
        });
        fetch("/celebraciones/vistas", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          body,
          keepalive: true
        }).then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
        }).catch((error) => console.error("No se pudieron confirmar las felicitaciones vistas.", error));
      }
    };

    if (celebrationTitle && celebrationDescription) {
      const index = Math.floor(Math.random() * encouragements.length);
      celebrationTitle.textContent = encouragements[index];
      celebrationDescription.textContent = "Cecyto celebra contigo cada avance que ya quedó registrado.";
    }
    celebrationDialog.hidden = false;
    document.body.classList.add("celebration-open");
    closeButtons.forEach((button) => button.addEventListener("click", closeCelebration));
    celebrationDialog.addEventListener("click", (event) => {
      if (event.target === celebrationDialog) closeCelebration();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !celebrationDialog.hidden) closeCelebration();
    });
    celebrationDialog.querySelector("[data-celebration-close]")?.focus();

    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const colors = ["#087443", "#b8e65b", "#e7bb3e", "#6dbb81"];
      const confetti = document.createDocumentFragment();
      for (let particleIndex = 0; particleIndex < 22; particleIndex += 1) {
        const particle = document.createElement("span");
        particle.className = "confetti-piece";
        particle.style.setProperty("--confetti-x", `${Math.random() * 100}%`);
        particle.style.setProperty("--confetti-delay", `${Math.random() * 0.45}s`);
        particle.style.setProperty("--confetti-color", colors[particleIndex % colors.length]);
        confetti.append(particle);
      }
      celebrationDialog.append(confetti);
      window.setTimeout(() => {
        celebrationDialog.querySelectorAll(".confetti-piece").forEach((piece) => piece.remove());
      }, 2400);
    }
  }

  const miniCalendar = document.querySelector("[data-student-mini-calendar]");
  if (miniCalendar) {
    const miniCalendarGrid = miniCalendar.querySelector("[data-mini-calendar-grid]");
    const miniCalendarMonth = miniCalendar.querySelector("[data-mini-calendar-month]");
    if (miniCalendarGrid && miniCalendarMonth) {
      const displayedMonth = new Date();
      displayedMonth.setDate(1);
      const deadlines = new Set(
        [...document.querySelectorAll("[data-deadline]")]
          .map((item) => item.dataset.deadline)
          .filter(Boolean)
      );
      const weekdays = ["Lu", "Ma", "Mi", "Ju", "Vi", "Sa", "Do"];
      const renderMiniCalendar = () => {
        const year = displayedMonth.getFullYear();
        const month = displayedMonth.getMonth();
        miniCalendarMonth.textContent = displayedMonth.toLocaleDateString("es-MX", {
          month: "long",
          year: "numeric"
        });
        const cells = weekdays.map((weekday) => {
          const heading = document.createElement("span");
          heading.textContent = weekday;
          heading.setAttribute("aria-hidden", "true");
          return heading;
        });
        const firstWeekday = (displayedMonth.getDay() + 6) % 7;
        for (let blank = 0; blank < firstWeekday; blank += 1) {
          const emptyDay = document.createElement("span");
          emptyDay.className = "is-empty";
          emptyDay.setAttribute("aria-hidden", "true");
          cells.push(emptyDay);
        }
        const daysInMonth = new Date(year, month + 1, 0).getDate();
        const today = new Date();
        for (let day = 1; day <= daysInMonth; day += 1) {
          const cell = document.createElement("button");
          cell.type = "button";
          cell.textContent = String(day);
          cell.setAttribute("aria-label", `${day} de ${displayedMonth.toLocaleDateString("es-MX", {
            month: "long",
            year: "numeric"
          })}`);
          const isoDate = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
          if (deadlines.has(isoDate)) {
            cell.classList.add("has-deadline");
            cell.setAttribute("aria-label", `${cell.getAttribute("aria-label")}, fecha de entrega`);
          }
          if (year === today.getFullYear() && month === today.getMonth() && day === today.getDate()) {
            cell.classList.add("is-today");
            cell.setAttribute("aria-current", "date");
          }
          cells.push(cell);
        }
        miniCalendarGrid.replaceChildren(...cells);
      };
      miniCalendar.querySelector("[data-mini-calendar-prev]")?.addEventListener("click", () => {
        displayedMonth.setMonth(displayedMonth.getMonth() - 1);
        renderMiniCalendar();
      });
      miniCalendar.querySelector("[data-mini-calendar-next]")?.addEventListener("click", () => {
        displayedMonth.setMonth(displayedMonth.getMonth() + 1);
        renderMiniCalendar();
      });
      renderMiniCalendar();
    }
  }

  const calendar = document.querySelector("[data-school-calendar]");
  const monthLabel = document.querySelector("[data-calendar-month]");
  if (calendar && monthLabel) {
    const calendarControls = document.querySelector("[data-calendar-controls]");
    const calendarLegend = document.querySelector("[data-calendar-legend]");
    const setCalendarView = (view) => {
      const monthView = view === "month";
      calendar.dataset.calendarView = monthView ? "month" : "list";
      calendar.hidden = !monthView;
      if (calendarControls) calendarControls.hidden = !monthView;
      if (calendarLegend) calendarLegend.hidden = !monthView;
      document.querySelectorAll("[data-calendar-view-toggle]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.calendarViewToggle === calendar.dataset.calendarView));
      });
    };
    document.querySelectorAll("[data-calendar-view-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        setCalendarView(button.dataset.calendarViewToggle || "month");
        if (eventsTitle) {
          eventsTitle.textContent = calendar.dataset.calendarView === "list"
            ? "Todas las actividades y avisos"
            : "Actividades y avisos con fecha";
        }
      });
    });
    setCalendarView(calendar.dataset.calendarView || "month");

    const displayedMonth = new Date();
    displayedMonth.setDate(1);
    const calendarEvents = JSON.parse(calendar.dataset.calendarEvents || "[]");
    const eventsByDate = new Map();
    calendarEvents.forEach((event) => {
      if (!eventsByDate.has(event.date)) eventsByDate.set(event.date, []);
      eventsByDate.get(event.date).push(event);
    });
    const eventList = document.querySelector("[data-calendar-event-list]");
    const eventCards = [...(eventList?.querySelectorAll("[data-calendar-event]") || [])];
    const emptyMessage = eventList?.querySelector("[data-calendar-empty]");
    if (emptyMessage) emptyMessage.hidden = eventCards.length > 0;
    const eventsTitle = document.querySelector("[data-calendar-events-title]");
    const clearFilter = document.querySelector("[data-calendar-clear]");
    let selectedDate = null;

    const renderCalendar = () => {
      const year = displayedMonth.getFullYear();
      const month = displayedMonth.getMonth();
      const label = displayedMonth.toLocaleDateString("es-MX", {
        month: "long",
        year: "numeric"
      });
      monthLabel.textContent = label.charAt(0).toUpperCase() + label.slice(1);

      const weekdays = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
      calendar.replaceChildren(...weekdays.map((day) => {
        const heading = document.createElement("span");
        heading.className = "calendar-weekday";
        heading.textContent = day;
        return heading;
      }));

      const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
      for (let blank = 0; blank < firstWeekday; blank += 1) {
        const emptyDay = document.createElement("span");
        emptyDay.className = "calendar-day is-empty";
        emptyDay.setAttribute("aria-hidden", "true");
        calendar.append(emptyDay);
      }

      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const today = new Date();
      for (let day = 1; day <= daysInMonth; day += 1) {
        const date = document.createElement("button");
        date.type = "button";
        date.className = "calendar-day";
        date.textContent = day;
        date.setAttribute("aria-pressed", "false");
        date.setAttribute("aria-label", `${day} de ${label}`);
        const isoDate = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const dayEvents = eventsByDate.get(isoDate) || [];
        date.setAttribute(
          "aria-label",
          dayEvents.length
            ? `${day} de ${label}, ${dayEvents.length} evento(s): ${dayEvents.map((event) => event.title).join(", ")}`
            : `${day} de ${label}, sin eventos`
        );
        if (dayEvents.length) {
          date.classList.add("has-event");
          const states = new Set(dayEvents.map((event) => event.state));
          const types = new Set(dayEvents.map((event) => event.type));
          ["overdue", "completed", "upcoming", "past"].forEach((state) => {
            if (states.has(state)) date.classList.add(`has-${state}`);
          });
          ["activity", "important"].forEach((type) => {
            if (types.has(type)) date.classList.add(`has-${type}`);
          });
          const markers = document.createElement("span");
          markers.className = "calendar-day-markers";
          markers.setAttribute("aria-hidden", "true");
          dayEvents.slice(0, 4).forEach((event) => {
            const marker = document.createElement("i");
            const markerState = event.type === "important"
              ? "important"
              : event.state === "completed" ? "completed"
                : event.state === "overdue" ? "overdue" : "upcoming";
            marker.className = `calendar-marker-dot marker-${markerState}`;
            markers.append(marker);
          });
          date.append(markers);
        }
        date.addEventListener("click", () => {
          selectedDate = isoDate;
          eventCards.forEach((card) => {
            card.hidden = card.dataset.eventDate !== selectedDate;
          });
          if (eventsTitle) {
            const selected = new Date(`${isoDate}T00:00:00`);
            const formattedDate = selected.toLocaleDateString("es-MX", {
              day: "numeric", month: "long", year: "numeric"
            });
            eventsTitle.textContent = dayEvents.length
              ? `Eventos del ${formattedDate}`
              : `Sin eventos el ${formattedDate}`;
          }
          const matchingEvents = eventList?.querySelector("[data-calendar-event]:not([hidden])");
          if (emptyMessage) emptyMessage.hidden = Boolean(matchingEvents);
          if (clearFilter) clearFilter.hidden = false;
          calendar.querySelectorAll(".calendar-day.is-selected").forEach((item) => {
            item.classList.remove("is-selected");
            item.setAttribute("aria-pressed", "false");
          });
          date.classList.add("is-selected");
          date.setAttribute("aria-pressed", "true");
        });
        if (year === today.getFullYear() && month === today.getMonth() && day === today.getDate()) {
          date.classList.add("is-today");
          date.setAttribute("aria-current", "date");
        }
        calendar.append(date);
      }
    };

    clearFilter?.addEventListener("click", () => {
      selectedDate = null;
      eventCards.forEach((card) => { card.hidden = false; });
      if (emptyMessage) emptyMessage.hidden = eventCards.length > 0;
      if (eventsTitle) {
        eventsTitle.textContent = calendar.dataset.calendarView === "list"
          ? "Todas las actividades y avisos"
          : "Actividades y avisos con fecha";
      }
      if (clearFilter) clearFilter.hidden = true;
      calendar.querySelectorAll(".calendar-day.is-selected").forEach((item) => {
        item.classList.remove("is-selected");
        item.setAttribute("aria-pressed", "false");
      });
    });

    document.querySelector("[data-calendar-prev]")?.addEventListener("click", () => {
      displayedMonth.setMonth(displayedMonth.getMonth() - 1);
      selectedDate = null;
      clearFilter?.click();
      renderCalendar();
    });
    document.querySelector("[data-calendar-next]")?.addEventListener("click", () => {
      displayedMonth.setMonth(displayedMonth.getMonth() + 1);
      selectedDate = null;
      clearFilter?.click();
      renderCalendar();
    });
    renderCalendar();
  }

  document.querySelectorAll(".flash").forEach((element) => {
    setTimeout(() => element.classList.add("fade"), 4500);
  });
});
