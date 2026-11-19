const $ = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short", hour12: false});
const imageUrl = (encoded) => `data:image/webp;base64,${encoded}`;

export function initGallery(bridge) {
  const gallery = $("gallery");
  const grid = $("gallery-grid");
  const dialog = $("gallery-dialog");
  const preview = $("gallery-preview-image");
  const previewStatus = $("gallery-preview-status");
  const download = $("gallery-download");
  let page = 1, pages = 1, listRequest = 0, previewRequest = 0, searchTimer, selected;

  function metadata(item) {
    const mode = item.backend === "api" ? "OpenAI API" : "Codex 订阅";
    return [mode, item.model, dateFormat.format(item.created * 1000)].filter(Boolean).join(" · ");
  }

  async function showPreview(item) {
    selected = item;
    const requestId = ++previewRequest;
    preview.hidden = !item.thumbnail;
    if (item.thumbnail) preview.src = imageUrl(item.thumbnail);
    $("gallery-preview-meta").textContent = `${metadata(item)} · ${item.width} × ${item.height}`;
    $("gallery-preview-prompt").textContent = item.prompt;
    previewStatus.textContent = "正在加载预览…";
    download.disabled = false;
    dialog.showModal();
    try {
      const result = await bridge.apiGet("dashboard/gallery-preview", {id: item.id});
      if (requestId !== previewRequest) return;
      preview.src = imageUrl(result.image);
      preview.hidden = false;
      previewStatus.textContent = "";
    } catch (error) {
      if (requestId === previewRequest) previewStatus.textContent = error.message || "图片预览加载失败。";
    }
  }

  function card(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "gallery-item";
    const thumb = document.createElement(item.thumbnail ? "img" : "span");
    if (item.thumbnail) {
      thumb.alt = "";
      thumb.loading = "lazy";
      thumb.src = imageUrl(item.thumbnail);
    } else {
      thumb.className = "gallery-item-placeholder";
      thumb.textContent = "可下载原图";
    }
    const info = document.createElement("span");
    info.className = "gallery-item-info";
    const title = document.createElement("span");
    title.className = "gallery-item-prompt";
    title.textContent = item.prompt || "生成的图片";
    const meta = document.createElement("span");
    meta.className = "gallery-item-meta";
    meta.textContent = metadata(item);
    info.append(title, meta);
    button.append(thumb, info);
    button.addEventListener("click", () => void showPreview(item));
    return button;
  }

  function pageControls(loading = false) {
    $("gallery-pages").hidden = pages <= 1;
    $("gallery-page-number").textContent = `第 ${page} / ${pages} 页`;
    $("gallery-prev").disabled = loading || page <= 1;
    $("gallery-next").disabled = loading || page >= pages;
  }

  async function load(targetPage) {
    clearTimeout(searchTimer);
    const requestId = ++listRequest;
    const status = $("gallery-status");
    const query = $("gallery-search").value.trim();
    const backend = $("gallery-backend").value;
    status.textContent = "正在加载图库…";
    pageControls(true);
    try {
      const result = await bridge.apiGet("dashboard/gallery", {page: targetPage, q: query, backend});
      if (requestId !== listRequest) return;
      page = result.page;
      pages = result.pages;
      grid.replaceChildren(...result.items.map(card));
      if (result.total) status.textContent = `共 ${result.total} 张`;
      else if (query || backend) status.textContent = "没有符合条件的图片。";
      else status.textContent = result.enabled ? "图库暂无图片。新生成的图片会出现在这里。" : "图库暂无图片。自动收录已关闭。";
      pageControls();
    } catch (error) {
      if (requestId !== listRequest) return;
      grid.replaceChildren();
      page = pages = 1;
      pageControls();
      status.textContent = error.message || "无法加载图库，请刷新重试。";
    }
  }

  function selectGallery(open) {
    gallery.hidden = !open;
    $("workspace").hidden = open;
    $("notice").classList.toggle("workspace-notice-hidden", open);
    $("open-workspace").setAttribute("aria-pressed", String(!open));
    $("open-gallery").setAttribute("aria-pressed", String(open));
    if (open) void load(1);
    else {
      clearTimeout(searchTimer);
      listRequest++;
      grid.replaceChildren();
    }
  }

  $("open-gallery").disabled = false;
  $("open-gallery").addEventListener("click", () => selectGallery(true));
  $("open-workspace").addEventListener("click", () => selectGallery(false));
  $("gallery-refresh").addEventListener("click", () => void load(page));
  $("gallery-backend").addEventListener("change", () => void load(1));
  $("gallery-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    listRequest++;
    pageControls(true);
    searchTimer = setTimeout(() => void load(1), 250);
  });
  $("gallery-prev").addEventListener("click", () => void load(page - 1));
  $("gallery-next").addEventListener("click", () => void load(page + 1));
  dialog.addEventListener("close", () => {
    selected = null;
    previewRequest++;
    preview.removeAttribute("src");
  });
  download.addEventListener("click", async () => {
    const item = selected;
    if (!item) return;
    download.disabled = true;
    try {
      await bridge.download("dashboard/gallery-download", {id: item.id});
    } catch (error) {
      if (selected === item) previewStatus.textContent = error.message || "原图下载失败。";
    } finally {
      if (selected === item) download.disabled = false;
    }
  });
}
