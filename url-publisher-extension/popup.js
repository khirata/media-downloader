document.addEventListener('DOMContentLoaded', async () => {
  const urlListEl = document.getElementById('urlList');
  const emptyStateEl = document.getElementById('emptyState');
  const publishAllBtn = document.getElementById('publishAllBtn');
  const addManualBtn = document.getElementById('addManualBtn');
  const settingsBtn = document.getElementById('settingsBtn');
  const globalDescriptionEl = document.getElementById('globalDescription');

  let urls = [];

  // Initialize
  await loadUrls();
  await addCurrentTabUrl();
  renderList();

  // Listeners
  settingsBtn.addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });

  addManualBtn.addEventListener('click', () => {
    urls.unshift('https://');
    saveUrls();
    renderList();
  });

  publishAllBtn.addEventListener('click', async () => {
    publishAllBtn.disabled = true;
    publishAllBtn.textContent = 'Publishing...';

    // Publish all items at once with the global description
    const desc = globalDescriptionEl.value.trim();
    const payload = { urls: urls };
    if (desc) {
      payload.description = desc;
    }

    const success = await publishPayload(payload);
    if (success) {
      urls = [];
      globalDescriptionEl.value = '';
    }

    await saveUrls();
    renderList();
    publishAllBtn.textContent = 'Publish All';
    publishAllBtn.disabled = urls.length === 0;
  });

  async function loadUrls() {
    return new Promise((resolve) => {
      chrome.storage.local.get(['urlStack', 'globalDesc'], (result) => {
        urls = result.urlStack || [];
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
        globalDesc: globalDescriptionEl.value
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
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs && tabs[0] && tabs[0].url) {
          const tabUrl = tabs[0].url;
          // Don't add if it's already a chrome extension page or already in stack
          if (!tabUrl.startsWith('chrome://') && !tabUrl.startsWith('chrome-extension://')) {
            if (!urls.includes(tabUrl)) {
              urls.unshift(tabUrl);
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
    } else {
      emptyStateEl.style.display = 'none';
      publishAllBtn.disabled = false;

      urls.forEach((url, index) => {
        const itemEl = document.createElement('div');
        itemEl.className = 'url-item';

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'url-input';
        input.value = url;
        input.addEventListener('change', (e) => {
          urls[index] = e.target.value;
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
          const payload = { urls: [urls[index]] };
          if (desc) {
            payload.description = desc;
          }

          const success = await publishPayload(payload);
          if (success) {
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
