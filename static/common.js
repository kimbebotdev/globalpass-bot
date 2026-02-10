const GlobalpassCommon = (() => {
  const isoToMmddyyyy = (val) => {
    if (!val) return "";
    if (val.includes("/")) return val;
    const parts = val.split("-");
    if (parts.length === 3) {
      return `${parts[1].padStart(2, "0")}/${parts[2].padStart(2, "0")}/${parts[0]}`;
    }
    return val;
  };

  const mmddyyyyToIso = (val) => {
    if (!val) return "";
    if (val.includes("-")) return val;
    const parts = val.split("/");
    if (parts.length === 3) {
      return `${parts[2]}-${parts[0].padStart(2, "0")}-${parts[1].padStart(2, "0")}`;
    }
    return val;
  };

  const showToast = (message, type = "error") => {
    if (!message || typeof Toastify !== "function") return;
    Toastify({
      text: message,
      duration: 3500,
      close: true,
      gravity: "top",
      position: "right",
      style: {
        background: type === "success" ? "#16a34a" : "#dc2626",
      },
    }).showToast();
  };

  return {
    isoToMmddyyyy,
    mmddyyyyToIso,
    showToast,
  };
})();

window.GlobalpassCommon = GlobalpassCommon;
