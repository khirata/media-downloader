console.log('URL Publisher Extension Background Service Worker Loaded');

chrome.runtime.onInstalled.addListener(() => {
    console.log('Extension installed');
});
