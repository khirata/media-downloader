document.addEventListener('DOMContentLoaded', async () => {
  const urlListEl = document.getElementById('urlList');
  const emptyStateEl = document.getElementById('emptyState');
  const publishAllBtn = document.getElementById('publishAllBtn');
  const clearAllBtn = document.getElementById('clearAllBtn');
  const addManualBtn = document.getElementById('addManualBtn');
  const settingsBtn = document.getElementById('settingsBtn');
  const globalDescriptionEl = document.getElementById('globalDescription');
  // Force re-download. Deliberately not persisted alongside urlStack/globalDesc:
  // a flag that survived the popup closing would silently re-download everything
  // on every later publish. It resets when the popup closes and after Publish All.
  const forceRedownloadEl = document.getElementById('forceRedownload');

  // Cap on how many just-published URLs we remember, so the list can't grow
  // unbounded when the popup stays open across many publishes.
  const MAX_REMEMBERED_PUBLISHED = 50;
  // Hex chars kept from each SHA-256 digest. 64 bits keeps collisions out of a
  // list this small (~1e-16 across the cap) while staying compact in storage.
  const URL_HASH_LENGTH = 16;

  let urls = [];
  // Hashes of URLs from recent successful publishes. The current tab is skipped
  // once if its hash appears here, so publishing doesn't immediately re-stack
  // the page you are on when the popup reopens. Stored hashed rather than plain:
  // matching is exact equality either way, and unlike urlStack this list
  // deliberately outlives the visible stack, so there's no reason to leave a
  // durable record of published URLs sitting in extension storage.
  let publishedHashes = [];

  // Initialize
  await loadUrls();
  await addCurrentTabUrl();
  renderList();

  // Listeners
  settingsBtn.addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });

  clearAllBtn.addEventListener('click', () => {
    urls = [];
    saveUrls();
    renderList();
  });

  addManualBtn.addEventListener('click', () => {
    urls.unshift({ url: 'https://', title: '' });
    saveUrls();
    renderList();
  });

  publishAllBtn.addEventListener('click', async () => {
    publishAllBtn.disabled = true;
    publishAllBtn.textContent = 'Publishing...';

    // Publish all items at once with the global description
    const desc = globalDescriptionEl.value.trim();
    const publishedUrls = urls.map(item => item.url);
    const payload = { urls: publishedUrls };
    if (desc) {
      payload.description = desc;
    }
    if (forceRedownloadEl.checked) {
      payload.force = true;
    }

    const success = await publishPayload(payload);
    if (success) {
      await rememberPublished(publishedUrls);
      urls = [];
      globalDescriptionEl.value = '';
      forceRedownloadEl.checked = false;
    }

    await saveUrls();
    renderList();
    publishAllBtn.textContent = 'Publish All';
    publishAllBtn.disabled = urls.length === 0;
  });

  async function loadUrls() {
    return new Promise((resolve) => {
      chrome.storage.local.get(['urlStack', 'globalDesc', 'publishedHashes'], (result) => {
        const stored = result.urlStack || [];
        // Migrate legacy string entries to {url, title} objects
        urls = stored.map(item =>
          typeof item === 'string' ? { url: item, title: '' } : item
        );
        publishedHashes = result.publishedHashes || [];
        if (result.globalDesc) {
          globalDescriptionEl.value = result.globalDesc;
        }
        resolve();
      });
    });
  }

  async function saveUrls() {
    return new Promise((resolve) => {
      chrome.storage.local.set({
        urlStack: urls,
        globalDesc: globalDescriptionEl.value,
        publishedHashes: publishedHashes
      }, () => {
        resolve();
      });
    });
  }

  globalDescriptionEl.addEventListener('change', () => {
    saveUrls();
  });

  async function addCurrentTabUrl() {
    return new Promise((resolve) => {
      chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
        if (tabs && tabs[0] && tabs[0].url) {
          const tabUrl = tabs[0].url;
          const tabTitle = tabs[0].title || '';
          // Don't add if it's already a chrome extension page or already in stack
          if (!tabUrl.startsWith('chrome://') && !tabUrl.startsWith('chrome-extension://')) {
            const tabHash = await hashUrl(tabUrl);
            if (publishedHashes.includes(tabHash)) {
              // Just published this page — skip it this once, then forget it so
              // reopening the popup later on the same page stacks it again.
              publishedHashes = publishedHashes.filter(h => h !== tabHash);
              saveUrls().then(resolve);
              return;
            }
            if (!urls.some(item => item.url === tabUrl)) {
              urls.unshift({ url: tabUrl, title: tabTitle });
              saveUrls().then(resolve);
              return;
            }
          }
        }
        resolve();
      });
    });
  }

  function renderList() {
    urlListEl.innerHTML = '';

    if (urls.length === 0) {
      emptyStateEl.style.display = 'block';
      publishAllBtn.disabled = true;
      clearAllBtn.disabled = true;
    } else {
      emptyStateEl.style.display = 'none';
      publishAllBtn.disabled = false;
      clearAllBtn.disabled = false;

      urls.forEach((item, index) => {
        const itemEl = document.createElement('div');
        itemEl.className = 'url-item';

        if (item.title) {
          const titleEl = document.createElement('div');
          titleEl.className = 'url-title';
          titleEl.textContent = item.title;
          titleEl.title = item.title;
          itemEl.appendChild(titleEl);
        }

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'url-input';
        input.value = item.url;
        input.addEventListener('change', (e) => {
          urls[index] = { ...urls[index], url: e.target.value };
          saveUrls();
        });

        const actionsEl = document.createElement('div');
        actionsEl.className = 'url-actions';

        const reorderEl = document.createElement('div');
        reorderEl.className = 'reorder-btns';

        const upBtn = document.createElement('button');
        upBtn.className = 'action-btn';
        upBtn.innerHTML = '↑';
        upBtn.disabled = index === 0;
        upBtn.onclick = () => moveUrl(index, -1);

        const downBtn = document.createElement('button');
        downBtn.className = 'action-btn';
        downBtn.innerHTML = '↓';
        downBtn.disabled = index === urls.length - 1;
        downBtn.onclick = () => moveUrl(index, 1);

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'action-btn delete';
        deleteBtn.innerHTML = '🗑️';
        deleteBtn.onclick = () => removeUrl(index);

        reorderEl.appendChild(upBtn);
        reorderEl.appendChild(downBtn);
        reorderEl.appendChild(deleteBtn);

        const publishBtn = document.createElement('button');
        publishBtn.className = 'item-publish-btn';
        publishBtn.textContent = 'Publish';
        publishBtn.onclick = async () => {
          itemEl.classList.add('publishing');
          publishBtn.textContent = '...';

          const desc = globalDescriptionEl.value.trim();
          const itemUrl = urls[index].url;
          const payload = { urls: [itemUrl] };
          if (desc) {
            payload.description = desc;
          }
          if (forceRedownloadEl.checked) {
            payload.force = true;
          }

          const success = await publishPayload(payload);
          if (success) {
            await rememberPublished([itemUrl]);
            removeUrl(index);
          } else {
            itemEl.classList.remove('publishing');
            publishBtn.textContent = 'Retry';
            publishBtn.style.backgroundColor = 'var(--danger-color)';
          }
        };

        actionsEl.appendChild(reorderEl);
        actionsEl.appendChild(publishBtn);

        itemEl.appendChild(input);
        itemEl.appendChild(actionsEl);

        urlListEl.appendChild(itemEl);
      });
    }
  }

  function moveUrl(index, dir) {
    const target = index + dir;
    if (target >= 0 && target < urls.length) {
      const temp = urls[index];
      urls[index] = urls[target];
      urls[target] = temp;
      saveUrls();
      renderList();
    }
  }

  // Truncated SHA-256 of a URL, used as the suppression key. Exact-match only —
  // any difference in the URL, however small, yields a different hash.
  async function hashUrl(url) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(url));
    return Array.from(new Uint8Array(digest))
      .map(byte => byte.toString(16).padStart(2, '0'))
      .join('')
      .slice(0, URL_HASH_LENGTH);
  }

  // Records URLs from a successful publish so addCurrentTabUrl() won't
  // immediately re-stack the page the user is on. Callers persist via saveUrls().
  async function rememberPublished(publishedUrls) {
    const hashes = await Promise.all(publishedUrls.map(url => hashUrl(url)));
    const merged = [...publishedHashes, ...hashes];
    publishedHashes = [...new Set(merged)].slice(-MAX_REMEMBERED_PUBLISHED);
  }

  function removeUrl(index) {
    urls.splice(index, 1);
    saveUrls();
    renderList();
  }

  async function getSettings() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(['endpoint', 'apiKey'], (result) => {
        resolve(result);
      });
    });
  }

  async function publishPayload(payload) {
    try {
      const settings = await getSettings();
      if (!settings.endpoint) {
        alert('Please configure the API endpoint in settings first.');
        chrome.runtime.openOptionsPage();
        return false;
      }

      const headers = {
        'Content-Type': 'application/json'
      };

      if (settings.apiKey) {
        headers['x-api-key'] = settings.apiKey;
      }

      console.log('Publishing to', settings.endpoint, payload);

      const res = await fetch(settings.endpoint, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        return true;
      } else {
        console.error('Failed to publish', await res.text());
        return false;
      }
    } catch (e) {
      console.error('Error publishing payload', e);
      return false;
    }
  }
});
