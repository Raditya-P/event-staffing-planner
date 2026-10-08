/* Landing and privacy pages: fill in this server's MCP address and contact. */
(async function () {
  "use strict";
  let me = {};
  try { me = await (await fetch("/api/me")).json(); } catch (e) { return; }
  const url = document.getElementById("mcp-url");
  if (url && me.mcp_url) url.textContent = me.mcp_url;
  const contact = document.getElementById("contact");
  if (contact && me.contact) {
    const a = document.createElement("a");
    a.href = "mailto:" + me.contact;
    a.textContent = me.contact;
    contact.textContent = "";
    contact.appendChild(a);
  }
  const copy = document.getElementById("copy");
  if (copy && navigator.clipboard) copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(me.mcp_url);
    copy.textContent = "Copied";
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
})();
