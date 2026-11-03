// SPDX-License-Identifier: AGPL-3.0-or-later
// Cookie banners answered on their own (harvester.browser.consent), in every frame of the job's
// and the recorder's browsers that dismiss them: "Reject all", else "Only necessary". Only two
// kinds of element are ever clicked:
//
// - a known consent manager's button, inside its own banner (MANAGERS: its container, and the
//   container's open shadow root; for a manager whose banner is a frame of its own, as
//   Sourcepoint's, the document of a frame that holds the manager's own elements), by its
//   selector or by one of the exact texts below;
// - a button whose whole text is one of the exact texts below (TEXTS), inside an element that
//   looks like a cookie banner: a dialog (role dialog or alertdialog, aria-modal, an open
//   <dialog>) or an element named cookie, consent or gdpr that floats (fixed or sticky), and whose
//   text speaks of cookies.
//
// Each button is clicked once; the page's root carries data-harvester-consent while the click is
// dispatched, so that recorder.js records no step of it, and noted in window.__harvesterConsent
// ({name, text}: the manager, or "banner", and the button's text), which a job's run reads for
// its progress log (consent.py). The pages are swept as they change, for a minute after they load.
(() => {
	if (window.__harvesterConsent) return
	window.__harvesterConsent = []
	const MANAGERS = [
		{ name: 'OneTrust', banner: '#onetrust-banner-sdk, #onetrust-pc-sdk', buttons: ['#onetrust-reject-all-handler', '.ot-pc-refuse-all-handler'] },
		{ name: 'Cookiebot', banner: '#CybotCookiebotDialog', buttons: ['#CybotCookiebotDialogBodyButtonDecline', '#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll'] },
		{ name: 'Usercentrics', banner: '#usercentrics-root, #usercentrics-cmp-ui', buttons: ['[data-testid="uc-deny-all-button"]', 'button#deny'] },
		{ name: 'Didomi', banner: '#didomi-host, #didomi-notice, #didomi-popup', buttons: ['#didomi-notice-disagree-button', '.didomi-continue-without-agreeing'] },
		{ name: 'Quantcast', banner: '#qc-cmp2-container, .qc-cmp2-container, #qc-cmp2-ui', buttons: [] },
		{ name: 'TrustArc', banner: '#truste-consent-track, #truste-consent-content', buttons: ['#truste-consent-required', '#truste-consent-reject'] },
		{ name: 'Borlabs', banner: '#BorlabsCookieBox, #BorlabsCookieBoxWrap', buttons: ['[data-cookie-refuse]', '._brlbs-refuse-btn a', '._brlbs-refuse a', '[data-borlabs-cookie-actions="reject"]'] },
		{ name: 'Klaro', banner: '.klaro .cookie-notice, .klaro .cookie-modal, #klaro', buttons: ['.cn-decline', '.cm-btn-decline'] },
		// Sourcepoint's message is a frame of its own (sp_message_iframe_…, often at a site's own
		// name): its whole document is the banner, known by the choices' classes (13: reject all).
		{ name: 'Sourcepoint', frame: '[class*="sp_choice_type_"]', banner: 'body', buttons: ['.sp_choice_type_REJECT_ALL', '.sp_choice_type_13'] },
		{ name: 'CookieYes', banner: '.cky-consent-container, .cky-modal', buttons: ['.cky-btn-reject'] },
		{ name: 'CookieLawInfo', banner: '#cookie-law-info-bar', buttons: ['#cookie_action_close_header_reject'] },
		{ name: 'Complianz', banner: '#cmplz-cookiebanner-container, .cmplz-cookiebanner', buttons: ['.cmplz-btn.cmplz-deny'] },
		{ name: 'Osano', banner: '.osano-cm-window, .osano-cm-dialog', buttons: ['.osano-cm-denyAll', '.osano-cm-button--type_denyAll'] },
		{ name: 'Iubenda', banner: '#iubenda-cs-banner', buttons: ['.iubenda-cs-reject-btn'] },
		{ name: 'consentmanager', banner: '#cmpwrapper, #cmpbox', buttons: ['.cmpboxbtnno'] },
		{ name: 'Cookie Script', banner: '#cookiescript_injected, #cookiescript_injected_wrapper', buttons: ['#cookiescript_reject'] },
		{ name: 'Termly', banner: '#termly-code-snippet-support', buttons: ['[data-tid="banner-decline"]'] },
		{ name: 'Axeptio', banner: '#axeptio_overlay', buttons: ['#axeptio_btn_dismiss'] },
		{ name: 'Cookie Notice', banner: '#cookie-notice', buttons: ['#cn-refuse-cookie'] },
		{ name: 'Google Funding Choices', banner: '.fc-consent-root', buttons: ['.fc-cta-do-not-consent'] },
	]
	// The buttons' whole texts, lower case, in the order they are preferred: to reject all first,
	// then to accept only the necessary ones.
	const TEXTS = [
		'reject all', 'reject all cookies', 'decline all', 'deny all', 'refuse all', 'reject', 'decline', 'deny',
		'disagree', 'i do not agree', 'do not agree', 'do not consent', 'reject optional cookies', 'reject non-essential cookies',
		'reject additional cookies', 'continue without accepting',
		'alle ablehnen', 'alles ablehnen', 'alle cookies ablehnen', 'ablehnen', 'optionale cookies ablehnen',
		'nicht zustimmen', 'ich stimme nicht zu', 'nicht einwilligen', 'weiter ohne einwilligung', 'ohne einwilligung fortfahren',
		'ohne zustimmung fortfahren',
		'odrzuć wszystkie', 'odrzuć wszystko', 'odrzuć wszystkie pliki cookie', 'odrzuć', 'odrzucam',
		'nie zgadzam się', 'nie wyrażam zgody', 'kontynuuj bez akceptacji',
		'only necessary', 'necessary only', 'only necessary cookies', 'necessary cookies only',
		'use necessary cookies only', 'allow necessary cookies only', 'accept necessary', 'accept necessary cookies',
		'accept only necessary', 'accept only necessary cookies', 'only essential cookies', 'essential cookies only',
		'accept only essential cookies', 'accept essential cookies only', 'only required cookies',
		'nur notwendige', 'nur notwendige cookies', 'nur notwendige cookies akzeptieren', 'nur notwendige cookies zulassen',
		'notwendige cookies akzeptieren', 'nur erforderliche', 'nur erforderliche cookies',
		'nur erforderliche cookies akzeptieren', 'nur essenzielle cookies', 'nur essenzielle cookies akzeptieren',
		'nur essentielle cookies akzeptieren', 'nur technisch notwendige cookies',
		'nur technisch notwendige cookies akzeptieren',
		'tylko niezbędne', 'tylko niezbędne pliki cookie', 'akceptuj tylko niezbędne', 'akceptuję tylko niezbędne',
		'zezwól tylko na niezbędne', 'zezwól na niezbędne pliki cookie', 'tylko wymagane',
	]
	const BUTTONS = 'button, [role=button], a, input[type=button], input[type=submit]'
	const DIALOGS = '[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open]'
	const NAMED = '[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [id*=gdpr i], [class*=gdpr i]'
	const COOKIES = /cookie|ciasteczk/i
	// At most this many clicks per document: a banner that comes back is not answered forever.
	const CLICKS = 5
	const SWEEP_MS = 60000
	const clicked = new WeakSet()

	const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden'
	const textOf = (el) => (el.tagName === 'INPUT' ? el.value : el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase()

	// The best button among the scopes' (elements or shadow roots): by the manager's selectors in
	// their order, else by TEXTS in theirs.
	function best(scopes, selectors) {
		for (const selector of selectors) {
			for (const scope of scopes) {
				const found = [...scope.querySelectorAll(selector)].find((el) => shown(el) && !clicked.has(el))
				if (found) return found
			}
		}
		const candidates = scopes.flatMap((scope) => [...scope.querySelectorAll(BUTTONS)]).filter((el) => !clicked.has(el) && shown(el))
		let chosen = null
		let rank = TEXTS.length
		for (const el of candidates) {
			const at = TEXTS.indexOf(textOf(el))
			if (at >= 0 && at < rank) {
				chosen = el
				rank = at
			}
		}
		return chosen
	}

	function press(el, name) {
		clicked.add(el)
		const text = (el.tagName === 'INPUT' ? el.value : el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80)
		const root = document.documentElement
		root.setAttribute('data-harvester-consent', '')
		try {
			el.click()
		} finally {
			root.removeAttribute('data-harvester-consent')
		}
		window.__harvesterConsent.push({ name, text })
	}

	// An element named like a banner, or the floating element it is in (up to a few levels up).
	function floating(el) {
		for (let at = el, depth = 0; at && at.nodeType === 1 && depth < 6; at = at.parentElement, depth += 1) {
			const position = getComputedStyle(at).position
			if (position === 'fixed' || position === 'sticky') return at
		}
		return null
	}

	function sweep() {
		if (window.__harvesterConsent.length >= CLICKS || !document.documentElement) return
		for (const manager of MANAGERS) {
			// A manager whose banner is a frame: only in such a frame (never the page itself).
			if (manager.frame && (window === window.top || !document.querySelector(manager.frame))) continue
			for (const banner of document.querySelectorAll(manager.banner)) {
				const scopes = banner.shadowRoot ? [banner, banner.shadowRoot] : [banner]
				const button = best(scopes, manager.buttons)
				if (button) {
					press(button, manager.name)
					return
				}
			}
		}
		const banners = new Set([...document.querySelectorAll(DIALOGS)].filter(shown))
		for (const named of document.querySelectorAll(NAMED)) {
			const banner = floating(named)
			if (banner && shown(banner)) banners.add(banner)
		}
		for (const banner of banners) {
			if (!COOKIES.test(banner.innerText || '')) continue
			const button = best([banner], [])
			if (button) {
				press(button, 'banner')
				return
			}
		}
	}

	const until = Date.now() + SWEEP_MS
	let timer = 0
	const soon = () => {
		if (timer || Date.now() > until) return
		timer = setTimeout(() => {
			timer = 0
			try {
				sweep()
			} catch (error) {
				// A page that changed under the sweep: the next change sweeps again.
			}
		}, 200)
	}
	const watcher = new MutationObserver(soon)
	watcher.observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'open'] })
	setTimeout(() => watcher.disconnect(), SWEEP_MS)
	document.addEventListener('DOMContentLoaded', soon)
	window.addEventListener('load', soon)
	soon()
})()
