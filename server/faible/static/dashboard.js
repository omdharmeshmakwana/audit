const selector = document.getElementById("artist");
const feed = document.getElementById("feed");
const overview = document.getElementById("overview");
const sync = document.getElementById("sync");
const previous = document.getElementById("previous");
const next = document.getElementById("next");
const pageStatus = document.getElementById("page-status");
const pageSize = 50;
let offset = 0;
let busy = false;
let selectionVersion = 0;
const historyCards = new Map();
let openSummaries = new Set();
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
const credits = (values) => Object.entries(values).map(([currency, amount]) => `${amount} ${currency}`).join(" · ") || "No reported spend";
const date = (value) => value ? new Date(value).toLocaleString() : "No activity yet";
function table(headers, rows) {
  const result = node("table");
  const head = node("tr");
  for (const header of headers) head.append(node("th", header));
  const body = node("tbody");
  for (const values of rows) {
    const row = node("tr");
    for (const value of values) row.append(node("td", value));
    body.append(row);
  }
  result.append(head, body);
  return result;
}
function renderSummary(artist) {
  const card = node("article", null, "panel artist-card");
  card.dataset.artistId = artist.id;
  const link = node("a", artist.email);
  link.href = `?artist_id=${encodeURIComponent(artist.id)}`;
  link.addEventListener("click", (event) => {
    event.preventDefault();
    selector.value = artist.id;
    selectArtist();
  });
  const heading = node("h2");
  heading.append(link);
  card.append(heading, node("p", `${artist.display_name} · ${artist.role}`, "muted"));
  const metrics = node("div", null, "artist-metrics");
  for (const [label, value] of [
    ["Active shot", artist.active_shot || "No shot selected"],
    ["Generations", artist.generation_count],
    ["Reported spend", credits(artist.credits)],
    ["Unknown costs", artist.unknown_cost_count],
    ["Running", artist.statuses.running || 0],
    ["Queued", artist.statuses.queued || 0],
    ["Completed", artist.statuses.succeeded || 0],
    ["Failed / cancelled", `${artist.statuses.failed || 0} / ${artist.statuses.cancelled || 0}`],
  ]) {
    const metric = node("div");
    metric.append(node("span", label, "muted"), node("strong", String(value)));
    metrics.append(metric);
  }
  card.append(metrics, node("p", `Last generation: ${date(artist.last_generation_at)}`, "muted"));
  card.append(node("p", `Last login: ${date(artist.last_login_at)} · Shot selected: ${date(artist.active_shot_set_at)}`, "muted"));
  const details = node("details");
  details.open = openSummaries.has(artist.id);
  details.append(node("summary", "Models and shots used"));
  details.append(node("h3", "Models"), table(["Service / model", "Requests", "Reported spend"], artist.models.map((model) => [`${model.service} / ${model.model_id || "Unknown model"}`, model.count, credits(model.credits)])));
  details.append(node("h3", "Shots"), table(["Shot", "Requests", "Reported spend"], artist.shots.map((shot) => [shot.shot_code, shot.count, credits(shot.credits)])));
  card.append(details);
  return card;
}
async function refresh() {
  if (busy) return;
  busy = true;
  const version = selectionVersion;
  try {
    const params = new URLSearchParams({ offset: String(offset), limit: String(pageSize) });
    if (selector.value) params.set("artist_id", selector.value);
    const response = await fetch(`/api/v1/dashboard?${params}`, { cache: "no-store" });
    if (response.status === 401) { location.href = "/login"; return; }
    if (!response.ok) throw new Error("Dashboard unavailable");
    const data = await response.json();
    if (version !== selectionVersion) return;
    const selected = selector.value;
    selector.replaceChildren(node("option", data.is_admin ? "All artists" : "My account"));
    selector.firstChild.value = "";
    for (const artist of data.artists) {
      const option = node("option", artist.email);
      option.value = artist.id;
      selector.append(option);
    }
    selector.value = selected;
    selector.disabled = !data.is_admin;
    openSummaries = new Set([...overview.querySelectorAll(".artist-card")].filter((card) => card.querySelector("details")?.open).map((card) => card.dataset.artistId));
    overview.replaceChildren(...data.summaries.map(renderSummary));
    const sections = [];
    for (const activity of data.activities) {
      const signature = JSON.stringify(activity);
      const previousCard = historyCards.get(activity.id);
      if (previousCard?.signature === signature) {
        sections.push(previousCard.card);
        continue;
      }
      const card = node("article", null, "panel history-card");
      card.append(node("strong", `${activity.email} · ${activity.shot_code} · ${activity.service} · ${activity.model_id || "Unknown model"}`));
      card.append(node("p", `${activity.status} · ${date(activity.started_at)} · ${activity.credits_cost ?? "Unknown"} ${activity.credits_currency}`));
      if (activity.warnings.length) card.append(node("div", `🔴 Prompt warning: ${activity.warnings.join(", ")}`, "error"));
      card.append(node("div", activity.prompt || "No prompt reported", "prompt"));
      const outputs = node("div", null, "dashboard-outputs");
      for (const output of activity.outputs) {
        if (!output.asset_id) continue;
        const link = node("a", `Open output #${output.index + 1}`);
        link.href = `/assets/${encodeURIComponent(output.asset_id)}`;
        const figure = node("figure");
        if (output.stored && output.media_type) {
          const video = output.media_type.startsWith("video");
          const preview = node(video ? "video" : "img");
          preview.className = "dashboard-preview";
          preview.src = `/api/v1/assets/${encodeURIComponent(output.asset_id)}/file`;
          if (video) { preview.controls = true; preview.preload = "metadata"; }
          else { preview.loading = "lazy"; preview.alt = "Generated output"; }
          figure.append(preview);
        }
        figure.append(link);
        outputs.append(figure);
      }
      card.append(outputs);
      const details = node("details");
      details.open = previousCard?.card.querySelector("details")?.open || false;
      details.append(node("summary", "Request settings, references and outputs"), node("pre", JSON.stringify({ job: activity.service_job_id, completed_at: activity.ended_at, seed: activity.seed, negative_prompt: activity.negative_prompt, parameters: activity.parameters, inputs: activity.inputs, outputs: activity.outputs }, null, 2)));
      card.append(details);
      historyCards.set(activity.id, { signature, card });
      sections.push(card);
    }
    const visibleIds = new Set(data.activities.map((activity) => activity.id));
    for (const id of historyCards.keys()) if (!visibleIds.has(id)) historyCards.delete(id);
    const desired = sections.length ? sections : [node("p", "No captured requests on this page.")];
    const keep = new Set(desired);
    for (const child of [...feed.children]) if (!keep.has(child)) child.remove();
    desired.forEach((card, index) => {
      if (feed.children[index] !== card) feed.insertBefore(card, feed.children[index] || null);
    });
    previous.disabled = offset === 0;
    next.disabled = !data.history.has_more;
    pageStatus.textContent = data.history.total ? `${offset + 1}–${offset + data.activities.length} of ${data.history.total}` : "No generations yet";
    sync.textContent = `● Live · updated ${new Date().toLocaleTimeString()} · summary totals cover the complete recorded history`;
  } catch {
    sync.textContent = "Connection interrupted — reconnecting automatically. Last data remains visible.";
  } finally {
    busy = false;
    if (version !== selectionVersion) void refresh();
  }
}
function selectArtist() {
  offset = 0;
  selectionVersion++;
  const url = new URL(location.href);
  if (selector.value) url.searchParams.set("artist_id", selector.value);
  else url.searchParams.delete("artist_id");
  history.replaceState(null, "", url);
  void refresh();
}
const initialArtist = new URL(location.href).searchParams.get("artist_id");
if (initialArtist) {
  const option = node("option", "Selected artist");
  option.value = initialArtist;
  selector.append(option);
  selector.value = initialArtist;
}
selector.addEventListener("change", selectArtist);
previous.addEventListener("click", () => { offset = Math.max(0, offset - pageSize); selectionVersion++; void refresh(); });
next.addEventListener("click", () => { offset += pageSize; selectionVersion++; void refresh(); });
void refresh();
setInterval(refresh, 2000);
