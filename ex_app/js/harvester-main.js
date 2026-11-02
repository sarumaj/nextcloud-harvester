// The app's page in Nextcloud: the user's jobs, a job's form (blank or from a template) with
// its account, the recorder (the app's browser, shown and driven from here), which proposes
// the links' patterns and shows what they match, and the addresses sent to on-demand jobs (Fetch
// a URL…, or ?fetch=<address> from the Send to Harvester bookmarklet). Plain JavaScript, no
// build: Nextcloud loads it into the page AppAPI renders for the top menu.
(function () {
	'use strict'

	const API = OC.generateUrl('/apps/app_api/proxy/harvester/api')
	// The recorder's WebSocket goes to HaRP directly (AppAPI's PHP proxy has none).
	const RECORDER = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${OC.webroot || ''}/exapps/harvester/ws/record`
	const VIEWPORT = { width: 1280, height: 800 }
	const BLANK = { name: '', source: 'site', target: '/Harvester', schedule: '0 6 * * *', options: {}, tags: [], enabled: true }
	// Usual schedules, offered below the job's schedule (cron, in the job's time zone).
	const SCHEDULES = [
		{ value: '*/15 * * * *', label: 'Every 15 minutes' },
		{ value: '*/30 * * * *', label: 'Every 30 minutes' },
		{ value: '0 * * * *', label: 'Hourly' },
		{ value: '0 */6 * * *', label: 'Every 6 hours' },
		{ value: '0 6 * * *', label: 'Daily 06:00' },
		{ value: '0 6 * * 1-5', label: 'Weekdays 06:00' },
		{ value: '0 6 * * 1', label: 'Mondays 06:00' },
		{ value: '0 6 1 * *', label: 'Monthly, the 1st' },
	]
	// A new job's schedule runs in the person's time zone (an IANA name, as the server takes it).
	const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
	let sources = []
	let presets = []
	// What a job's button met (a refused Stop, say), shown in its row once the list is drawn again.
	const notices = {}
	// Harvester's page in Nextcloud, which the bookmarklet opens with ?fetch=<the page's address>.
	const PAGE = `${location.origin}${OC.generateUrl('/apps/app_api/embedded/harvester/harvester')}`
	// Whether a job runs only when sent an address (no schedule), and whether its source has one.
	const onDemand = (job) => !job.schedule
	const addressOf = (source) => (sources.find((s) => s.name === source) || {}).address || ''
	// The options of a new job of the source whose address is the one given (a website's: its
	// recipe's listing).
	const withAddress = (source, url) => addressOf(source) === 'recipe'
		? { recipe: JSON.stringify({ start: url }, null, 2) }
		: { [addressOf(source)]: url }
	// The job's own address, if it has one (an on-demand job's may be empty: each fetch brings one).
	function ownAddress(job) {
		const option = addressOf(job.source)
		if (option !== 'recipe') return String(job.options[option] || '').trim()
		try {
			return String(JSON.parse(job.options.recipe || '{}').start || '').trim()
		} catch (e) {
			return ''
		}
	}

	async function api(method, path, body) {
		const response = await fetch(API + path, {
			method,
			headers: { requesttoken: OC.requestToken, 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body),
		})
		const data = await response.json().catch(() => ({}))
		if (!response.ok) {
			const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(', ') : data.detail
			throw new Error(detail || response.statusText)
		}
		return data
	}

	function el(tag, attributes = {}, ...children) {
		const node = document.createElement(tag)
		for (const [key, value] of Object.entries(attributes)) {
			if (key.startsWith('on')) node.addEventListener(key.slice(2), value)
			else if (key === 'text') node.textContent = value
			else node.setAttribute(key, value)
		}
		node.append(...children)
		return node
	}

	function field(label, input, help) {
		return el('label', { class: 'harvester-field' }, el('span', { text: label }), input,
			help ? el('small', { text: help }) : '')
	}

	// A size given in a unit (bytes, KB, MB, GB) in the largest unit it fills: 1500000 bytes is
	// "= 1.5 MB". Shown in units of 1000, as the progress log gives the files' sizes, so a limit
	// reads as the sizes it is compared with; the option's own unit is the server's, which counts
	// an option's MB as 1024 × 1024 bytes (4096 MB is "≈ 4.3 GB").
	const SIZE_UNITS = ['bytes', 'KB', 'MB', 'GB', 'TB']
	function sizeHint(value, unit) {
		const number = Number(value)
		if (value === '' || !Number.isFinite(number) || number < 0) return ''
		let bytes = number * 1024 ** Math.max(0, SIZE_UNITS.indexOf(unit))
		let index = 0
		while (bytes >= 1000 && index < SIZE_UNITS.length - 1) {
			bytes /= 1000
			index += 1
		}
		const rounded = Math.round(bytes * 10) / 10
		return `${rounded === bytes ? '=' : '≈'} ${rounded.toLocaleString()} ${SIZE_UNITS[index]}`
	}
	window.__harvesterSizeHint = sizeHint

	// Each source's icon (24 × 24, drawn in the text's colour): a globe, an envelope, a feed, a
	// play button, a picture, a cloud, a tag.
	const ICONS = {
		site: [['circle', { cx: 12, cy: 12, r: 9 }], ['ellipse', { cx: 12, cy: 12, rx: 4, ry: 9 }], ['path', { d: 'M3 12h18M5 7h14M5 17h14' }]],
		mail: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 2 }], ['path', { d: 'M3 7l9 6 9-6' }]],
		feed: [['circle', { cx: 6, cy: 18, r: 1.5 }], ['path', { d: 'M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14' }]],
		media: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 3 }], ['path', { d: 'M10 9l5 3-5 3z' }]],
		gallery: [['rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }], ['circle', { cx: 9, cy: 9, r: 1.5 }], ['path', { d: 'M4 18l5-5 4 4 3-3 4 4' }]],
		remote: [['path', { d: 'M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.1 9.1 4.5 4.5 0 0 0 7 18z' }]],
		releases: [['path', { d: 'M3 12V4h8l10 10-8 8z' }], ['circle', { cx: 7.5, cy: 7.5, r: 1.5 }]],
	}
	function icon(source) {
		const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
		for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: 'harvester-icon', 'aria-hidden': 'true' })) {
			svg.setAttribute(name, String(value))
		}
		for (const [tag, attributes] of ICONS[source] || ICONS.site) {
			const shape = document.createElementNS('http://www.w3.org/2000/svg', tag)
			for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, String(value))
			svg.append(shape)
		}
		return svg
	}

	function when(timestamp) {
		return timestamp ? new Date(timestamp * 1000).toLocaleString() : 'never'
	}

	// The host of the innermost frame a step acts in ("" for a frame without an address of its own).
	function frameHost(step) {
		const frames = Array.isArray(step.frame) ? step.frame : []
		try {
			return frames.length && frames[frames.length - 1].url ? new URL(frames[frames.length - 1].url).hostname : ''
		} catch (e) {
			return ''
		}
	}

	function describe(step) {
		const action = describeAction(step)
		return step.frame && step.frame.length ? `${action} ⧉ in frame ${frameHost(step) || '(no address)'}` : action
	}

	// Clicks whose text reads like a consent banner's button: a banner shows on the first visit,
	// often not on the job's later ones, so the panel suggests making them optional (never on its own).
	const CONSENT = /^(?:ok(?:ay)?|got it|agree|i agree|accept(?: all)?(?: cookies)?|allow(?: all)?(?: cookies)?|akzeptieren|alle akzeptieren|alles akzeptieren|zustimmen|alle zulassen|einverstanden|zgadzam się|akceptuj(?:ę)?(?: wszystkie)?|accepter(?: tout)?|tout accepter)(?=$|[\s!.,])/iu
	const looksLikeConsent = (step) => step.action === 'click' && CONSENT.test(String(step.text || '').trim())

	function describeAction(step) {
		if (step.action === 'goto') return `Open ${step.url}`
		if (step.action === 'scroll') return `Scroll to load more${step.rounds ? ` (at most ${step.rounds} times)` : ''}`
		if (step.action === 'click') return `Click ${step.text ? `“${step.text}”` : step.selector}`
		if (step.action === 'press') return `Press ${step.key} in ${step.selector}`
		if (step.credential === 'totp') return `Fill in the current one-time code (${step.selector})`
		if (step.credential) return `Fill in the account's ${step.credential} (${step.selector})`
		return `Fill in “${step.value}” (${step.selector})`
	}

	// What everyone's jobs and recordings hold now, without whose: "Runners 2/2 · Browsers 3/3
	// (2 jobs, 1 recording) · 3 queued". Empty when the app did not say.
	function statusText(status) {
		if (!status || !status.runners || !status.browsers) return ''
		const { runners, browsers } = status
		const plural = (number, word) => `${number} ${word}${number === 1 ? '' : 's'}`
		const open = browsers.jobs + browsers.recordings
		const held = open ? ` (${[browsers.jobs ? plural(browsers.jobs, 'job') : '', browsers.recordings ? plural(browsers.recordings, 'recording') : ''].filter(Boolean).join(', ')})` : ''
		return `Runners ${runners.busy}/${runners.total} · Browsers ${open}/${browsers.total}${held} · ${status.queued} queued`
	}

	async function renderJobs(root) {
		// Another view opened while the list was asked for (Edit, say) stays: the list is not drawn.
		const view = root.firstChild
		const [jobs, status] = await Promise.all([api('GET', '/jobs'), api('GET', '/status').catch(() => null)])
		if (root.firstChild !== view) return
		// A job's button: off until its answer, which (or its error, beside the job) the list
		// drawn again shows; a run that ended just before Stop, say, is refused with a reason.
		const act = (job, path, method = 'POST') => async (event) => {
			event.currentTarget.disabled = true
			try {
				await api(method, `/jobs/${job.id}${path}`)
			} catch (e) {
				notices[job.id] = e.message
			}
			await show(root)
		}
		const table = el('table', { class: 'harvester-jobs' },
			el('thead', {}, el('tr', {}, ...['Job', 'Folder', 'Schedule', 'Tags', 'Last run', ''].map((h) => el('th', { text: h })))))
		const body = el('tbody', {})
		for (const job of jobs) {
			const state = job.running ? (job.paused ? 'held' : 'running') : job.queued ? 'queued' : job.last_status || 'never'
			const label = { running: 'Running', held: 'Paused', queued: 'Queued', ok: 'OK', failed: 'Failed', auth: 'Sign-in refused', challenge: 'Needs you', never: 'Not run yet' }[state] || state
			body.append(el('tr', {},
				el('td', { class: 'harvester-with-icon', title: (sources.find((x) => x.name === job.source) || {}).title || job.source },
					icon(job.source), el('strong', { text: job.name }),
					// Not "paused": that is a running job held by Pause.
					job.enabled ? '' : el('span', { class: 'harvester-chip', text: 'off', title: 'Not enabled: runs only with Run now' })),
				el('td', { class: 'harvester-muted', text: job.target }),
				el('td', {}, onDemand(job)
					? el('span', { class: 'harvester-chip harvester-on-demand', text: 'On demand', title: 'No schedule: runs when you send it an address' })
					: el('code', { text: job.schedule }),
				job.once ? el('span', { class: 'harvester-chip', text: 'once', title: 'Deleted after a run that went through; its files stay' }) : ''),
				el('td', {}, ...job.tags.map((tag) => el('span', { class: 'harvester-chip', text: tag }))),
				el('td', {},
					el('span', { class: `harvester-status harvester-status-${state}`, text: label }),
					el('div', {
						class: 'harvester-muted harvester-message',
						'data-job-message': String(job.id),
						text: job.running ? job.waiting || 'Starting'
							: job.queued ? job.waiting || 'Waiting for its site or a free runner'
								: job.last_run ? `${when(job.last_run)} · ${job.last_message || ''}` : '',
					}),
					notices[job.id] ? el('div', { class: 'harvester-error harvester-message', role: 'alert', text: notices[job.id] }) : '',
					fetchList(job, act)),
				el('td', { class: 'harvester-actions' },
					state === 'challenge' ? el('button', { class: 'primary', text: 'Continue in the browser', onclick: () => openSession(root, job) }) : '',
					// While it runs: pause (or resume) and stop instead of Run now; a stopped run
					// keeps what it saved, and the next run goes on from there.
					...(job.running
						? [
							el('button', { text: job.paused ? 'Resume' : 'Pause', onclick: act(job, job.paused ? '/resume' : '/pause') }),
							el('button', { text: 'Stop', onclick: act(job, '/stop') }),
						]
						: [onDemand(job) && !ownAddress(job) ? ''
							: job.queued
								? el('button', { text: 'Run now', disabled: '', title: 'Queued already: it runs once its turn comes' })
								: el('button', { text: 'Run now', onclick: act(job, '/run') })]),
					onDemand(job) ? el('button', { text: 'Fetch a URL…', 'aria-expanded': 'false', onclick: (event) => fetchForm(event.currentTarget, job) }) : '',
					el('button', { text: 'Edit', onclick: () => renderForm(root, job) }),
					el('button', {
						class: 'error',
						text: 'Delete',
						onclick: (event) => {
							if (confirm(`Delete ${job.name}? The files it downloaded stay.`)) act(job, '', 'DELETE')(event)
						},
					}))))
		}
		// Shown once: the next drawing of the list is without them.
		for (const id of Object.keys(notices)) delete notices[id]
		table.append(body)
		// Everyone's load, so a job that waits is understood; updated by the progress log's polls.
		const load = el('p', { class: 'harvester-muted harvester-load', role: 'status', title: 'All people’s jobs and recordings', text: statusText(status) })
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: 'Harvester' }),
					el('p', { class: 'harvester-muted', text: 'Fetches files from websites into your folders on a schedule, and tags them.' })),
				el('button', { class: 'primary', text: 'New job', onclick: () => renderStart(root) })),
			load,
			jobs.length ? table : el('div', { class: 'harvester-empty' },
				el('p', { text: 'No jobs yet.' }),
				el('p', { class: 'harvester-muted', text: 'A job fetches the files of one site: record it in the browser, or start from a template.' })),
			jobs.length ? progressPanel(root, jobs) : '',
			sendHelp())
	}

	// An on-demand job's waiting addresses below its message, oldest first: the one being fetched
	// (Stop ends it), the others each with Cancel.
	function fetchList(job, act) {
		if (!(job.fetches || []).length) return ''
		return el('ul', { class: 'harvester-fetches', 'aria-label': `Addresses ${job.name} waits to fetch` },
			...job.fetches.map((fetch) => el('li', {},
				el('code', { text: fetch.url }),
				fetch.running
					? el('span', { class: 'harvester-muted', text: ' fetching' })
					: el('button', { class: 'harvester-link', text: 'Cancel', 'aria-label': `Cancel the fetch of ${fetch.url}`, onclick: act(job, `/fetch/${fetch.id}`, 'DELETE') }))))
	}

	// Fetch a URL…: a row below the job's with the address to fetch; Fetch queues it (checked
	// first, as the job's source checks its own) and draws the list again.
	function fetchForm(button, job) {
		const row = button.closest('tr')
		const open = row.nextElementSibling && row.nextElementSibling.classList.contains('harvester-fetch-row')
		if (open) {
			row.nextElementSibling.remove()
			button.setAttribute('aria-expanded', 'false')
			return
		}
		button.setAttribute('aria-expanded', 'true')
		const root = button.closest('#harvester')
		const input = el('input', { type: 'url', required: '', placeholder: 'https://…', 'aria-label': `The address ${job.name} fetches`, spellcheck: 'false' })
		const error = el('p', { class: 'harvester-error', role: 'alert' })
		const form = el('form', {
			class: 'harvester-fetch',
			onsubmit: async (event) => {
				event.preventDefault()
				error.textContent = ''
				try {
					await sendTo(job, input.value)
				} catch (e) {
					error.textContent = e.message
					return
				}
				show(root)
			},
		},
		input,
		el('button', { class: 'primary', type: 'submit', text: 'Fetch' }),
		el('button', { type: 'button', text: 'Cancel', onclick: () => fetchForm(button, job) }))
		row.after(el('tr', { class: 'harvester-fetch-row' }, el('td', { colspan: '6' }, form,
			el('small', { class: 'harvester-muted', text: 'Fetched in place of the job’s own address, for this run alone; files it fetched before are not fetched again.' }), error)))
		input.focus()
	}

	// Queues the address for the on-demand job; the error says why it was refused.
	async function sendTo(job, url) {
		const answer = await api('POST', `/jobs/${job.id}/fetch`, { url: url.trim() })
		if (!answer.new) notices[job.id] = 'That address waits to be fetched already.'
		return answer
	}

	// Send to Harvester: a bookmarklet that opens this page with the page one is on (?fetch=).
	function sendHelp() {
		const code = `javascript:void window.open('${PAGE}?fetch='+encodeURIComponent(location.href))`
		return el('details', { class: 'harvester-send' },
			el('summary', { text: 'Send to Harvester from any page' }),
			el('p', { text: 'Drag this link to your bookmarks bar. On a page with a video, gallery, feed or files, click the bookmark: Harvester opens and asks which on-demand job fetches the page (or makes a new one from it).' }),
			el('a', {
				class: 'harvester-bookmarklet',
				href: code,
				title: 'Drag it to your bookmarks bar',
				// Nextcloud's page runs no script from a link: here it is only dragged.
				onclick: (event) => event.preventDefault(),
			}, 'Send to Harvester'))
	}

	// The page opened with ?fetch=<address> (the bookmarklet): which on-demand job fetches it, or
	// a new job of a source with an address, filled with it.
	async function renderSend(root, url) {
		const jobs = (await api('GET', '/jobs')).filter((job) => onDemand(job) && addressOf(job.source))
		const error = el('p', { class: 'harvester-error', role: 'alert' })
		const select = el('select', { 'aria-label': 'On-demand job' },
			...jobs.map((job) => el('option', { value: String(job.id), text: `${job.name} (${job.target})` })))
		const fetchButton = el('button', {
			class: 'primary',
			text: 'Fetch',
			onclick: async () => {
				fetchButton.disabled = true
				error.textContent = ''
				try {
					await sendTo(jobs.find((job) => String(job.id) === select.value), url)
				} catch (e) {
					error.textContent = e.message
					fetchButton.disabled = false
					return
				}
				show(root)
			},
		})
		// A new job: one card per source with an address, its form filled with this one.
		let host = ''
		try {
			host = new URL(url).hostname
		} catch (e) { /* not an address: the server says so on Fetch */ }
		const kinds = sources.filter((source) => source.address).map((source) => el('button', {
			class: 'harvester-with-icon',
			onclick: () => renderForm(root, null, { ...BLANK, name: host, source: source.name, schedule: '', options: withAddress(source.name, url) }, url),
		}, icon(source.name), el('span', { text: source.title })))
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {}, el('h2', { text: 'Send to Harvester' }), el('p', {}, el('code', { class: 'harvester-sent', text: url }))),
				el('button', { text: 'Back to the jobs', onclick: () => show(root) })),
			jobs.length
				? el('div', { class: 'harvester-card' }, field('Fetch it with', select, 'An on-demand job: the address takes the place of its own, for this run alone.'), fetchButton, error)
				: el('p', { class: 'harvester-muted', text: 'No on-demand job yet: make one from this address.' }),
			el('div', { class: 'harvester-card' },
				el('h3', { text: 'New job from this address' }),
				el('div', { class: 'harvester-send-kinds' }, ...kinds)))
		;(jobs.length ? fetchButton : root.querySelector('.harvester-send-kinds button')).focus()
	}

	// The jobs' progress log: what the runs do, live while one runs, of all jobs or of one.
	// Polled, often while a job runs or waits its turn; a run that is queued, starts or ends
	// redraws the list (its states and messages change).
	function progressPanel(root, jobs) {
		const select = el('select', { 'aria-label': 'Job' },
			el('option', { value: '', text: 'All jobs' }),
			...jobs.map((job) => el('option', { value: String(job.id), text: job.name })))
		try {
			const kept = localStorage.getItem('harvester-log-job') || ''
			if (jobs.some((job) => String(job.id) === kept)) select.value = kept
		} catch (e) { /* no storage: all jobs */ }
		const list = el('ol', { class: 'harvester-log', 'aria-live': 'polite' })
		const panel = el('section', { class: 'harvester-progress' },
			el('div', { class: 'harvester-progress-head' }, el('h3', { text: 'Progress' }), select), list)
		let after = 0
		let timer = null
		const running = jobs.filter((job) => job.running).map((job) => job.id)
		const queued = jobs.filter((job) => job.queued && !job.running).map((job) => job.id)
		// The on-demand jobs' waiting addresses, as the log's polls give them (by job, in order).
		const sorted = (fetches) => JSON.stringify(Object.keys(fetches).sort().map((id) => [id, fetches[id]]))
		const fetches = sorted(Object.fromEntries(jobs.filter((job) => (job.fetches || []).length).map((job) => [String(job.id), job.fetches.map((fetch) => fetch.id)])))
		const clock = (at) => {
			const date = new Date(at * 1000)
			return date.toDateString() === new Date().toDateString() ? date.toLocaleTimeString() : date.toLocaleString()
		}
		const line = (entry) => el('li', { class: `harvester-log-${entry.level}` },
			el('time', { text: clock(entry.at) }),
			select.value ? '' : el('span', { class: 'harvester-log-job', text: entry.name }),
			el('span', { class: 'harvester-log-text', text: entry.message },
				// The job's browser's picture of the failed step (or of the page asking for a
				// person): kept for its newest failure only, so an older line's may be gone.
				entry.shot ? el('button', {
					type: 'button',
					class: 'harvester-link',
					text: 'Show what the browser saw',
					onclick: () => showShot(root, jobs.find((job) => job.id === entry.job) || { id: entry.job, name: entry.name }),
				}) : ''))
		async function poll() {
			if (!document.body.contains(panel)) return
			try {
				const data = await api('GET', `/log?after=${after}${select.value ? `&job=${select.value}` : ''}`)
				// Another view opened while the log was asked for (a job's form): it is not replaced.
				if (!document.body.contains(panel)) return
				const waiting = (data.queued || []).filter((id) => !data.running.includes(id))
				if (String(data.running) !== String(running) || String(waiting) !== String(queued) || sorted(data.fetches || {}) !== fetches) {
					show(root)
					return
				}
				const load = root.querySelector('.harvester-load')
				if (load && data.status) load.textContent = statusText(data.status)
				// Why a queued job waits (its place, a runner, its site), or a running one (a browser).
				for (const [id, reason] of Object.entries(data.waiting || {})) {
					const message = root.querySelector(`[data-job-message="${id}"]`)
					if (message) message.textContent = reason
				}
				const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 24
				for (const entry of data.lines) {
					list.append(line(entry))
					after = entry.id
					const message = root.querySelector(`[data-job-message="${entry.job}"]`)
					if (message && running.includes(entry.job) && !(data.waiting || {})[entry.job]) message.textContent = entry.message
				}
				while (list.children.length > 300) list.firstChild.remove()
				if (!list.children.length) list.append(el('li', { class: 'harvester-muted', text: 'No runs yet.' }))
				else if (data.lines.length && list.firstChild.classList.contains('harvester-muted')) list.firstChild.remove()
				if (stick) list.scrollTop = list.scrollHeight
			} catch (e) { /* tried again below */ }
			timer = setTimeout(poll, running.length || queued.length ? 2000 : 10000)
		}
		select.addEventListener('change', () => {
			try { localStorage.setItem('harvester-log-job', select.value) } catch (e) { /* not kept */ }
			clearTimeout(timer)
			after = 0
			list.replaceChildren()
			poll()
		})
		// The first poll once the panel is on the page (it is returned before it is added).
		setTimeout(poll, 0)
		return panel
	}

	// A new job: blank (recorded in the browser) or from a template with its parameters.
	function renderStart(root) {
		const error = el('p', { class: 'harvester-error' })
		const cards = presets.map((preset) => {
			const inputs = {}
			const fields = Object.entries(preset.parameters).map(([name, spec]) => {
				inputs[name] = el('input', { value: spec.default, spellcheck: 'false' })
				const row = field(spec.title, inputs[name], spec.description)
				if (spec.suggestions && spec.suggestions.length) inputs[name].after(suggestionChips(inputs[name], spec.suggestions))
				return row
			})
			return el('div', { class: 'harvester-card' },
				el('h3', { class: 'harvester-with-icon' }, icon(preset.source), el('span', { text: preset.title })),
				el('p', { class: 'harvester-muted', text: preset.description }), ...fields,
				el('button', {
					class: 'primary',
					text: 'Use this template',
					onclick: async () => {
						try {
							const body = Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value]))
							renderForm(root, null, await api('POST', `/presets/${preset.id}`, body))
						} catch (e) {
							error.textContent = e.message
						}
					},
				}))
		})
		// One card per source: a blank job of it.
		const kinds = sources.map((source) => el('div', { class: 'harvester-card harvester-kind' },
			el('h3', { class: 'harvester-with-icon' }, icon(source.name), el('span', { text: source.title })),
			el('p', { class: 'harvester-muted', text: source.description }),
			el('button', { class: 'primary', text: 'Start', onclick: () => renderForm(root, null, { ...BLANK, source: source.name }) })))
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {}, el('h2', { text: 'New job' }), el('p', { class: 'harvester-muted', text: 'What should the job fetch?' })),
				el('button', { text: 'Back to the jobs', onclick: () => show(root) })),
			el('div', { class: 'harvester-kinds' }, ...kinds),
			cards.length ? el('h3', { text: 'Templates for known sites' }) : '',
			...cards, error)
		// The button that opened this view is gone: the keyboard goes on from the first choice.
		const first = root.querySelector('.harvester-kind button')
		if (first) first.focus()
	}

	// What a source's account is for, above its fields (each field says what it is used for).
	const ACCOUNT_USE = {
		site: 'The recorded sign-in’s fields get the username, password and current one-time code; or a signed-in browser’s cookies instead of a sign-in.',
		mail: 'The mailbox’s sign-in: a password, or OAuth2’s client and refresh token.',
		remote: 'rclone’s configuration of the remote, with its passwords or tokens.',
		releases: 'A token, for private repositories and for more requests than GitHub allows without one.',
	}

	// The job's account: its fields, saved with the form's Save (or here with Save account, for
	// an existing job); an empty field keeps what is stored. Sets container.typed() (whether a
	// field holds something) and container.save(job) (stores what was typed; false: nothing).
	async function renderCredentials(container, job, source, notice = '') {
		container.replaceChildren()
		container.typed = () => false
		container.save = async () => false
		if (!source.credentials.length) return
		const status = el('p', { class: 'harvester-muted', role: 'status', text: notice })
		const error = el('p', { class: 'harvester-error' })
		container.append(el('h3', { text: 'The account this job signs in with' }),
			el('p', { text: `${ACCOUNT_USE[source.name] || 'What the job signs in with where its source asks for it.'} Stored encrypted in Nextcloud; never shown again.` }))
		let stored = {}
		if (job) {
			try {
				stored = await api('GET', `/jobs/${job.id}/credentials`)
			} catch (e) {
				error.textContent = `Whether an account is stored is unknown: ${e.message}`
			}
		}
		const inputs = {}
		for (const name of source.credentials) {
			// Several lines (rclone's configuration, cookies) in a text area; secrets hidden.
			const placeholder = stored[name] ? '(stored, leave empty to keep)' : ''
			const [title, help] = (source.credential_help || {})[name] || [name, '']
			inputs[name] = ['config', 'cookies'].includes(name)
				? el('textarea', { rows: '6', class: 'harvester-recipe', placeholder, autocomplete: 'off', spellcheck: 'false', 'data-credential': name })
				: el('input', {
					type: ['password', 'token', 'secret', 'totp', 'client_secret', 'refresh_token'].includes(name) ? 'password' : 'text',
					placeholder,
					autocomplete: name === 'username' ? 'username' : 'off',
				})
			container.append(field(title, inputs[name], help))
			if (source.name === 'remote' && name === 'config') {
				// rclone's own questions instead of a configuration written elsewhere and pasted.
				const button = el('button', {
					type: 'button',
					text: 'Set up with rclone',
					onclick: () => {
						if (!button.nextElementSibling || !button.nextElementSibling.classList.contains('harvester-wizard')) button.after(rcloneWizard(inputs[name]))
					},
				})
				container.append(button)
			}
		}
		const typed = () => Object.fromEntries(Object.entries(inputs).filter(([, input]) => input.value).map(([k, input]) => [k, input.value]))
		container.typed = () => Object.keys(typed()).length > 0
		container.save = async (saved) => {
			const body = typed()
			if (!Object.keys(body).length) return false
			await api('PUT', `/jobs/${saved.id}/credentials`, body)
			return true
		}
		// A new job's account is stored with the job, by the form's Save.
		const button = job
			? el('button', {
				type: 'button',
				text: 'Save account',
				onclick: async () => {
					button.disabled = true
					error.textContent = ''
					status.textContent = ''
					try {
						const done = await container.save(job)
						await renderCredentials(container, job, source, done ? 'Account saved.' : 'Nothing typed: the stored account stays.')
					} catch (e) {
						error.textContent = e.message
						button.disabled = false
					}
				},
			})
			: el('p', { class: 'harvester-muted', text: 'Saved with the job when you press Save.' })
		container.append(error, button, status)
	}

	// The rclone wizard: the app runs `rclone config` with a configuration of its own and shows
	// rclone's questions one at a time (no terminal). The finished remote, checked as a run checks
	// it, goes into the account's field to be reviewed and saved with the job (or Save account).
	let wizardLists = 0
	function rcloneWizard(textarea) {
		const panel = el('div', { class: 'harvester-card harvester-wizard', 'aria-live': 'polite' })
		let session = null
		// The sign-in's connection, if one runs: the wizard's end ends it (and frees its browser).
		let signIn = null
		const error = el('p', { class: 'harvester-error' })
		const close = () => {
			if (signIn) signIn.close()
			if (session) api('DELETE', `/rclone/wizard/${session}`).catch(() => {})
			session = null
			panel.remove()
		}
		const cancel = el('button', { type: 'button', text: 'Cancel', onclick: close })
		// One step at a time: while rclone answers (it may take seconds), the buttons but Cancel
		// are off, so a second click does not meet "the wizard is busy".
		let busy = false
		async function step(method, path, body) {
			if (busy) return
			busy = true
			error.textContent = ''
			const buttons = [...panel.querySelectorAll('button')].filter((button) => button !== cancel && !button.disabled)
			buttons.forEach((button) => { button.disabled = true })
			try {
				render(await api(method, path, body))
			} catch (e) {
				error.textContent = e.message
				buttons.forEach((button) => { button.disabled = false })
			} finally {
				busy = false
			}
		}
		function render(view) {
			session = view.session
			if (view.done) showDone(view)
			else showQuestion(view)
		}

		async function chooseType() {
			const select = el('select', {})
			panel.replaceChildren(el('h3', { text: 'Set up with rclone' }),
				el('p', { class: 'harvester-muted', text: 'rclone asks for the remote’s settings one at a time, as “rclone config” does.' }),
				field('Kind of remote', select), error,
				el('button', { type: 'button', class: 'primary', text: 'Start', onclick: () => step('POST', '/rclone/wizard', { type: select.value }) }),
				cancel)
			try {
				for (const kind of await api('GET', '/rclone/types')) {
					select.append(el('option', { value: kind.name, text: kind.description === kind.name ? kind.name : `${kind.description} (${kind.name})` }))
				}
				select.value = 'webdav'
				select.focus()
			} catch (e) {
				error.textContent = e.message
			}
		}

		function showQuestion(view) {
			const question = view.question
			let input
			let suggestions = ''
			if (question.exclusive && question.examples.length) {
				input = el('select', {}, ...question.examples.map((example) =>
					el('option', { value: example.value, text: example.help ? `${example.value || '(empty)'}: ${example.help.split('\n')[0]}` : example.value })))
			} else if (question.type === 'bool') {
				input = el('select', {}, el('option', { value: 'true', text: 'Yes' }), el('option', { value: 'false', text: 'No' }))
			} else if (question.name === 'config_token') {
				input = el('textarea', { rows: '4', class: 'harvester-recipe', autocomplete: 'off', spellcheck: 'false' })
			} else {
				input = el('input', { type: question.password ? 'password' : 'text', autocomplete: question.password ? 'new-password' : 'off', spellcheck: 'false' })
				if (question.examples.length) {
					// Suggestions rclone knows; any other value may be typed.
					wizardLists += 1
					suggestions = el('datalist', { id: `harvester-wizard-list-${wizardLists}` },
						...question.examples.map((example) => el('option', { value: example.value, label: example.help.split('\n')[0] })))
					input.setAttribute('list', suggestions.id)
				}
			}
			input.value = view.value
			error.textContent = view.error || ''
			const back = el('button', {
				type: 'button',
				text: 'Back',
				// Before the first question: the choice of the remote's kind, with a new wizard.
				onclick: () => {
					if (!view.first) step('POST', `/rclone/wizard/${session}/back`)
					else {
						api('DELETE', `/rclone/wizard/${session}`).catch(() => {})
						session = null
						chooseType()
					}
				},
			})
			const form = el('form', {
				onsubmit: (event) => {
					event.preventDefault()
					step('POST', `/rclone/wizard/${session}/answer`, { state: view.state, result: input.value })
				},
			},
			field(question.name + (question.required ? ' (required)' : ''), input),
			suggestions,
			el('p', { class: 'harvester-wizard-help', text: question.help }),
			view.authorize
				? el('div', { class: 'harvester-wizard-authorize' },
					el('p', {},
						el('strong', { text: 'Sign in here: ' }),
						'the provider’s sign-in opens in Harvester’s own browser, shown below; rclone takes the token when you are done. ',
						'Some providers refuse to sign in in such a browser (Google may say “This browser or app may not be secure”): then use your own computer, as below.'),
					el('button', { type: 'button', text: 'Sign in here', onclick: () => signInHere(view) }),
					el('p', { text: 'Or on your own computer: with a web browser and rclone (the same version is best), run this, sign in, and paste everything it prints here:' }),
					el('pre', { class: 'harvester-code', text: view.authorize }))
				: '',
			error,
			el('button', { type: 'submit', class: 'primary', text: 'Next' }), back, cancel)
			panel.replaceChildren(el('h3', { text: `Set up with rclone: ${view.type}` }), form)
			input.focus()
		}

		// The provider's sign-in in the app's browser (the recorder's screen): the server runs
		// `rclone authorize`, and its token answers the question once the person signed in.
		function signInHere(view) {
			const socket = new WebSocket(`${RECORDER}?rclone=${encodeURIComponent(session)}`)
			signIn = socket
			socket.addEventListener('close', () => { if (signIn === socket) signIn = null })
			const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
			const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The sign-in’s browser' })
			const url = el('input', { class: 'harvester-url', readonly: '' })
			const message = el('p', { class: 'harvester-muted', text: 'Starting rclone and the browser…' })
			let finished = false
			const back = el('button', { type: 'button', text: 'Back to the question', onclick: () => { finished = true; socket.close(); showQuestion(view) } })
			socket.addEventListener('message', (event) => {
				const data = JSON.parse(event.data)
				if (data.type === 'frame') {
					if (left(screen, socket)) return
					screen.frame(data.data)
					if (message.textContent === 'Starting rclone and the browser…') message.textContent = ''
				} else if (data.type === 'cursor') screen.style.cursor = data.cursor
				else if (data.type === 'url') url.value = data.url
				else if (data.type === 'error') { message.textContent = data.message; screen.say(data.message) }
				else if (data.type === 'viewport') screen.resized(data)
				else if (data.type === 'rclone-done') {
					finished = true
					socket.close()
					render(data.view)
				}
			})
			socket.addEventListener('close', () => {
				if (!finished && panel.contains(screen)) {
					message.textContent ||= 'The browser closed.'
					screen.say(message.textContent)
				}
			})
			const box = wireScreen(screen, send)
			const container = el('div', { class: 'harvester-recorder' })
			container.append(
				el('p', { class: 'harvester-muted', text: 'Sign in and allow rclone’s access; the browser closes by itself once rclone has the token (at most ten minutes).' }),
				overlay(el('div', { class: 'harvester-toolbar' }, url, fullWindow(container, box, send), back), message),
				el('div', { class: 'harvester-stage' }, box))
			panel.replaceChildren(el('h3', { text: `Set up with rclone: sign in to ${view.type}` }), container)
			screen.focusKeys()
		}

		function showDone(view) {
			const back = el('button', { type: 'button', text: 'Back', onclick: () => step('POST', `/rclone/wizard/${session}/back`) })
			if (view.error) {
				panel.replaceChildren(el('h3', { text: 'Set up with rclone' }),
					el('p', { class: 'harvester-error', text: view.error }), error, back, cancel)
				return
			}
			panel.replaceChildren(el('h3', { text: 'Set up with rclone: done' }),
				el('p', { text: 'The configuration rclone wrote (passwords obscured, as rclone keeps them). Use it, then save it with the job’s Save (or Save account).' }),
				el('pre', { class: 'harvester-code harvester-wizard-result', text: view.config }),
				error,
				el('button', {
					type: 'button',
					class: 'primary',
					text: 'Use this configuration',
					onclick: () => {
						textarea.value = view.config
						close()
						textarea.focus()
					},
				}),
				back, cancel)
		}

		chooseType()
		return panel
	}

	// One pick in the recorder's panel (a level of links to follow, the file links, the next
	// page): its element (a CSS selector) and its pattern, both editable, what it matches on the
	// page, examples with their values, the proposed file name and tags; and the same controls for
	// each: Pick again (repick) and Remove.
	function renderLevel(title, spec, proposal, send, target, level, suggestions, repick) {
		const selector = el('input', { class: 'harvester-pattern', value: spec.selector || '', spellcheck: 'false' })
		selector.addEventListener('change', () => send({ type: 'selector', target, level, selector: selector.value }))
		const box = el('div', { class: 'harvester-level', 'data-target': target, 'data-level': String(level) },
			el('div', { class: 'harvester-level-head' }, el('h4', { text: title }),
				el('button', { type: 'button', text: 'Pick again', title: 'Pick this on the page again (in its picking mode)', onclick: () => repick(target, level) }),
				el('button', { type: 'button', class: 'error', text: 'Remove', title: target === 'follow' ? 'Remove this level (the levels after it move up)' : 'Remove this pick', onclick: () => send({ type: 'remove', target, level }) })),
			field(target === 'next' && spec.kind === 'number' ? 'The numbers’ container (CSS selector)' : 'Element (CSS selector)', selector))
		if (target === 'next') return renderNext(box, spec, proposal, send)
		const pattern = el('input', { class: 'harvester-pattern', value: spec.pattern || '', spellcheck: 'false' })
		pattern.addEventListener('change', () => send({ type: 'pattern', target, level, pattern: pattern.value }))
		box.append(field('Pattern (named groups become values)', pattern))
		if (!proposal) return box
		if (proposal.error) box.append(el('p', { class: 'harvester-error', text: proposal.error }))
		box.append(el('p', { text: `On this page: ${proposal.matching} of ${proposal.found} links match (solid outline; dashed: left out).` }))
		box.append(el('ul', {}, ...(proposal.examples || []).map((example) => el('li', {},
			el('span', { text: example.text || example.url }), ' ',
			...Object.entries(example.values).map(([k, v]) => el('span', { class: 'harvester-value', text: `${k}=${v}` }))))))
		if (target === 'links' && proposal.path) {
			const use = (label, apply) => el('button', { type: 'button', text: label, onclick: (e) => { apply(); e.target.textContent = '✔ will be used' } })
			box.append(el('p', { text: `Proposed file name: ${proposal.path}` }), use('Use it', () => { suggestions.path = proposal.path }))
			if (proposal.tags.length) {
				box.append(el('p', { text: `Proposed tags: ${proposal.tags.join(', ')}` }), use('Use them', () => { suggestions.tags = proposal.tags }))
			}
		}
		return box
	}

	// The next page: one link, or numbered pages (‹ 1 [2] 3 ›: the number one higher than the
	// current page's), with what this page shows and the switch between the two when both fit.
	function renderNext(box, spec, proposal, send) {
		const where = `On ${Number(spec.level || 0) === 0 ? 'the listing' : `the pages of level ${spec.level}`}.`
		if (spec.kind === 'number') {
			box.append(el('p', { text: `Numbered pages: the number after the current page’s is followed. ${where}` }))
			if (proposal && proposal.kind === 'number') {
				box.append(el('p', {
					text: `On this page: numbers ${proposal.numbers.join(' ')}; this is page ${proposal.current}, the next is ${proposal.following ?? 'none (the last page)'}.`,
				}))
			}
			if (spec.buttons) box.append(el('p', { class: 'harvester-muted', text: 'The numbers are buttons: the job clicks them in the browser.' }))
			if (proposal && proposal.link) box.append(el('button', { type: 'button', text: 'Use the picked link alone', onclick: () => send({ type: 'next-kind', kind: 'link' }) }))
		} else {
			box.append(el('p', { text: `The link to the next page. ${where}` }))
			if (proposal && proposal.number) box.append(el('button', { type: 'button', text: 'Use numbered pages', onclick: () => send({ type: 'next-kind', kind: 'number' }) }))
		}
		return box
	}

	// What the recipe's test found: the listing pages it read with the links matched, the first
	// file links with the file names the job would give them, the problems, and the page the
	// browser showed when a step failed.
	function renderTest(result) {
		const ended = {
			complete: 'It went through the whole site.',
			files: 'It stopped at the first file links (a test lists a few).',
			pages: 'It stopped after its pages (a test reads a few).',
			time: 'It stopped: its time was up.',
			stopped: 'Stopped.',
			failed: 'It failed.',
		}[result.ended] || ''
		const box = el('div', { class: 'harvester-test', 'aria-live': 'polite' },
			el('h3', { text: 'Test' }),
			el('p', { text: `${result.files} file links on ${result.pages.length} pages. ${ended}` }))
		if (result.http) box.append(el('p', { class: 'harvester-muted', text: 'The job reads these pages without the browser (no steps): a page built by JavaScript may show it fewer links than here. Turn on “Use the browser” then.' }))
		if (result.problems.length) box.append(el('ul', { class: 'harvester-error' }, ...result.problems.map((problem) => el('li', { text: problem }))))
		// Optional steps it went on without: no problems of the recipe, but worth knowing.
		const skipped = result.skipped || []
		if (skipped.length) {
			box.append(el('div', { class: 'harvester-skipped' },
				el('p', { class: 'harvester-muted', text: 'Optional steps skipped:' }),
				el('ul', {}, ...skipped.map((step) => el('li', { class: 'harvester-muted', text: `step ${step.number} (${step.label}): ${step.why}` })))))
		}
		box.append(el('ol', { class: 'harvester-test-pages' }, ...result.pages.map((page) => el('li', {},
			el('span', { text: `${page.level === 0 ? 'Listing' : `Level ${page.level}`}, page ${page.page}: ${page.matching} of ${page.found} ${page.kind === 'follow' ? 'links to follow' : 'file links'} ` }),
			el('code', { text: page.url }),
			page.files.length ? el('ul', {}, ...page.files.map((file) => el('li', {},
				file.problem ? el('span', { class: 'harvester-error', text: `${file.url}: ${file.problem}` }) : el('span', { text: file.path, title: file.url })))) : ''))))
		if (result.shot) {
			box.append(el('p', { text: `${result.shot.what}: ${result.shot.title || ''} ` }, el('code', { text: result.shot.url })),
				el('img', { class: 'harvester-shot-image', src: `data:image/jpeg;base64,${result.shot.image}`, alt: 'What the browser showed then' }))
		}
		return box
	}

	// Keys the remote browser gets as key presses: they move, delete or confirm rather than type.
	const NON_TEXT_KEYS = new Set(['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp',
		'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`)])

	// What a key press on the person's keyboard sends: a key press for the keys above and for
	// shortcuts (Ctrl or Cmd with a letter: Ctrl+A), null for everything that types, which the
	// browser turns into text with the person's own layout (AltGr, dead keys, input methods).
	// AltGr reports Ctrl+Alt on Windows: that is typing, not a shortcut. Ctrl+V pastes.
	function keyAction(event) {
		if (event.isComposing || ['Dead', 'Process', 'Unidentified', 'Shift', 'Control', 'Alt', 'AltGraph', 'Meta'].includes(event.key)) return null
		const altGraph = event.getModifierState('AltGraph') || (event.ctrlKey && event.altKey)
		const shortcut = (event.ctrlKey || event.metaKey) && !altGraph && event.key.length === 1 && event.key.toLowerCase() !== 'v'
		if (!NON_TEXT_KEYS.has(event.key) && !shortcut) return null
		const modifiers = [['Control', event.ctrlKey && !altGraph], ['Alt', event.altKey && !altGraph], ['Meta', event.metaKey], ['Shift', event.shiftKey]]
			.filter(([, on]) => on).map(([name]) => name)
		const key = shortcut ? event.key.toLowerCase() : event.key
		return { type: 'key', key: [...modifiers, key].join('+') }
	}
	window.__harvesterKeyAction = keyAction

	// The remote browser's screen: the person's mouse and wheel go to it at the browser's
	// coordinates; the keyboard types into an invisible text field over it (the person's layout),
	// whose text goes to the browser as text. Returns the box to put on the page.
	function wireScreen(screen, send) {
		const keys = el('textarea', {
			class: 'harvester-keys', 'aria-label': 'Keyboard of the remote browser',
			autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
		})
		// Until its first frame (or instead of it, when the browser cannot start or is taken),
		// the screen says what is going on, not an empty image.
		const notice = el('div', { class: 'harvester-screen-notice', text: 'Starting the browser…' })
		const box = el('div', { class: 'harvester-screen-box' }, screen, notice, keys)
		screen.frame = (data) => { screen.src = `data:image/jpeg;base64,${data}`; notice.hidden = true }
		screen.say = (text) => { if (!screen.getAttribute('src')) { notice.textContent = text; notice.hidden = false } }
		screen.focusKeys = () => keys.focus({ preventScroll: true })
		// The remote window's size, as the recorder last confirmed it ({type: viewport}).
		screen.size = { ...VIEWPORT }
		screen.resized = (data) => {
			screen.size = { width: data.width, height: data.height }
			screen.style.aspectRatio = `${data.width} / ${data.height}`
		}

		function point(event) {
			const area = screen.getBoundingClientRect()
			return { x: (event.clientX - area.left) * screen.size.width / area.width, y: (event.clientY - area.top) * screen.size.height / area.height }
		}
		let lastMove = 0
		screen.addEventListener('mousemove', (event) => {
			if (Date.now() - lastMove < 50) return
			lastMove = Date.now()
			send({ type: 'mouse', event: 'move', ...point(event) })
		})
		for (const kind of ['down', 'up']) {
			screen.addEventListener(`mouse${kind}`, (event) => {
				event.preventDefault()
				screen.focusKeys()
				send({ type: 'mouse', event: kind, button: event.button === 2 ? 'right' : 'left', ...point(event) })
			})
		}
		screen.addEventListener('contextmenu', (event) => event.preventDefault())
		// The wheel's movement added up and sent once a frame, in pixels (a trackpad fires dozens
		// of events a second; one message each queued up behind the browser: a late, jerky scroll).
		let wheel = null
		screen.addEventListener('wheel', (event) => {
			event.preventDefault()
			const unit = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? screen.size.height : 1
			const scale = screen.size.height / (screen.getBoundingClientRect().height || screen.size.height)
			if (!wheel) {
				wheel = { dx: 0, dy: 0 }
				requestAnimationFrame(() => {
					send({ type: 'wheel', dx: Math.round(wheel.dx), dy: Math.round(wheel.dy) })
					wheel = null
				})
			}
			wheel.dx += event.deltaX * unit * scale
			wheel.dy += event.deltaY * unit * scale
		}, { passive: false })

		keys.addEventListener('keydown', (event) => {
			const action = keyAction(event)
			if (!action) return // typing: the text arrives as input
			event.preventDefault()
			send(action)
		})
		// The typed text, once composed (dead keys and input methods finish in compositionend).
		const flush = () => {
			const text = keys.value
			keys.value = ''
			if (text) send({ type: 'text', text })
		}
		keys.addEventListener('input', (event) => { if (!event.isComposing) flush() })
		keys.addEventListener('compositionend', flush)
		keys.addEventListener('paste', (event) => {
			event.preventDefault()
			send({ type: 'text', text: event.clipboardData.getData('text') })
		})
		return box
	}

	// The recorder's view over the whole window, with its controls floating over the screen:
	// the remote browser takes the view's size (pages lay out for it, not scaled), and back
	// to the usual size on leaving. Returns the switch's button.
	function fullWindow(container, box, send) {
		let timer = null
		const fit = () => {
			if (!container.classList.contains('harvester-wide')) return
			send({ type: 'viewport', width: Math.round(box.clientWidth), height: Math.round(box.clientHeight) })
		}
		const resized = () => { clearTimeout(timer); timer = setTimeout(fit, 300) }
		// Drawn, not a character: fonts often lack ⛶.
		const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
		svg.setAttribute('viewBox', '0 0 24 24')
		svg.setAttribute('width', '18')
		svg.setAttribute('height', '18')
		svg.setAttribute('aria-hidden', 'true')
		const path = document.createElementNS('http://www.w3.org/2000/svg', 'path')
		path.setAttribute('d', 'M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5')
		path.setAttribute('fill', 'none')
		path.setAttribute('stroke', 'currentColor')
		path.setAttribute('stroke-width', '2')
		svg.append(path)
		const button = el('button', { class: 'harvester-wide-switch', title: 'Full window (again: back)', 'aria-label': 'Full window' }, svg)
		const leave = () => {
			container.classList.remove('harvester-wide')
			button.setAttribute('aria-pressed', 'false')
			window.removeEventListener('resize', resized)
			send({ type: 'viewport', ...VIEWPORT })
		}
		// The browser's full screen puts the view above everything (Nextcloud's header too,
		// which no z-index inside the page can cover); Esc leaves it as usual.
		document.addEventListener('fullscreenchange', () => {
			if (!document.fullscreenElement && container.classList.contains('harvester-wide')) leave()
			else resized()
		})
		button.addEventListener('click', () => {
			if (container.classList.contains('harvester-wide')) {
				if (document.fullscreenElement) document.exitFullscreen().catch(() => leave())
				else leave()
				return
			}
			container.classList.add('harvester-wide')
			button.setAttribute('aria-pressed', 'true')
			window.addEventListener('resize', resized)
			const request = container.requestFullscreen ? container.requestFullscreen() : Promise.reject(new Error('none'))
			// Without the browser's full screen (refused, or none), the page's own full window.
			request.catch(() => null).finally(() => requestAnimationFrame(fit))
		})
		return button
	}

	// The controls over the screen in a full window: the bar collapses to one button.
	function overlay(...children) {
		const bar = el('div', { class: 'harvester-overlay' }, ...children)
		const collapse = el('button', {
			class: 'harvester-overlay-toggle',
			text: '▾',
			title: 'Hide or show the controls',
			'aria-label': 'Hide or show the controls',
			onclick: () => bar.classList.toggle('harvester-overlay-hidden'),
		})
		bar.prepend(collapse)
		return bar
	}

	// A browser's view that is no longer on the page (another view of the app was opened without
	// its Cancel): its connection closes, which ends the browser on the server and frees it.
	function left(screen, socket) {
		if (screen.isConnected) return false
		socket.close()
		return true
	}

	// Which address the sites see this browser come from (the server's, or the proxy's), as the
	// server asked Cloudflare through the browser's own connection: the proof that a proxy is used.
	// The browser's cookies (the browser views' Cookies panel; the recorder's jar.py): grouped by
	// domain, the page's site's or all, values hidden until "Show" (a value is often the sign-in
	// itself); each to change or delete, a domain's all at once, one to add. intoAccount(text,
	// count) takes a cookies.txt of the shown cookies into the job's account (null: not offered);
	// keep offers "Use for this job" (the browser's session kept for the job, the browser open).
	// Returns { button, box, handle(data) } (handle: whether the message was the panel's).
	function cookiesPanel(send, intoAccount, keep) {
		let everything = false
		let host = ''
		let groups = []
		const values = {}
		const key = (cookie) => JSON.stringify([cookie.name, cookie.domain, cookie.path])
		const status = el('p', { class: 'harvester-muted', role: 'status' })
		const error = el('p', { class: 'harvester-error' })
		const list = el('div', { class: 'harvester-cookie-groups' })
		const form = el('div', { class: 'harvester-cookie-form' })
		const scope = el('select', { 'aria-label': 'Which cookies' },
			el('option', { value: 'site', text: 'This site’s' }), el('option', { value: 'all', text: 'All' }))
		const load = () => send({ type: 'cookies', all: everything })
		scope.addEventListener('change', () => { everything = scope.value === 'all'; load() })
		const readable = (expires) => expires ? new Date(expires * 1000).toLocaleString() : 'session'

		// Add (cookie null) or change a cookie. A value left empty while changing keeps the one the
		// panel never showed.
		function edit(cookie) {
			const input = (value, attributes = {}) => { const node = el('input', attributes); node.value = value; return node }
			const name = input(cookie ? cookie.name : '', { 'aria-label': 'Name' })
			const value = input(cookie && key(cookie) in values ? values[key(cookie)] : '', {
				'aria-label': 'Value', placeholder: cookie && !(key(cookie) in values) ? '(unchanged)' : '', autocomplete: 'off', spellcheck: 'false',
			})
			const domain = input(cookie ? cookie.domain : host, { 'aria-label': 'Domain' })
			const path = input(cookie ? cookie.path : '/', { 'aria-label': 'Path' })
			const expiresAt = cookie && cookie.expires ? new Date(cookie.expires * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ''
			const expires = input(expiresAt, { type: 'datetime-local', 'aria-label': 'Expires (empty: the session)' })
			const secure = el('input', { type: 'checkbox' })
			secure.checked = cookie ? cookie.secure : true
			const httpOnly = el('input', { type: 'checkbox' })
			httpOnly.checked = Boolean(cookie && cookie.httpOnly)
			const sameSite = el('select', { 'aria-label': 'SameSite' }, ...['Lax', 'Strict', 'None'].map((v) => el('option', { value: v, text: v })))
			sameSite.value = cookie ? cookie.sameSite || 'Lax' : 'Lax'
			const save = () => {
				const wanted = {
					name: name.value.trim(),
					value: cookie && !value.value && !(key(cookie) in values) ? null : value.value,
					domain: domain.value.trim(),
					path: path.value.trim() || '/',
					expires: expires.value ? Math.floor(new Date(expires.value).getTime() / 1000) : null,
					secure: secure.checked,
					httpOnly: httpOnly.checked,
					sameSite: sameSite.value,
				}
				const message = { type: 'cookie-set', cookie: wanted, all: everything }
				if (cookie) message.original = { name: cookie.name, domain: cookie.domain, path: cookie.path }
				send(message)
				form.replaceChildren()
			}
			form.replaceChildren(el('h4', { text: cookie ? `Change ${cookie.name}` : 'Add a cookie' }),
				field('Name', name), field('Value', value), field('Domain', domain, 'This site, a parent (example.com) or a subdomain; a leading dot: its subdomains too.'),
				field('Path', path), field('Expires', expires, 'Empty: until the browser closes (a session cookie).'),
				el('label', {}, secure, ' Secure '), el('label', {}, httpOnly, ' HttpOnly '), field('SameSite', sameSite),
				el('div', { class: 'harvester-toolbar' },
					el('button', { type: 'button', class: 'primary', text: cookie ? 'Save cookie' : 'Add cookie', onclick: save }),
					el('button', { type: 'button', text: 'Cancel', onclick: () => form.replaceChildren() })))
		}

		function render() {
			list.replaceChildren(...(groups.length ? groups : [null]).map((group) => {
				if (!group) return el('p', { class: 'harvester-muted', text: everything ? 'No cookies.' : `No cookies for ${host || 'this page'}.` })
				return el('div', { class: 'harvester-cookie-group', 'data-domain': group.domain },
					el('div', { class: 'harvester-level-head' }, el('h4', { text: group.domain }),
						el('button', { type: 'button', class: 'error', text: 'Delete all', title: `Delete every cookie of ${group.domain}`, onclick: () => send({ type: 'cookies-delete-site', domain: group.domain, all: everything }) })),
					el('table', { class: 'harvester-cookies' },
						el('tr', {}, ...['Name', 'Value', 'Path', 'Expires', 'Flags', ''].map((text) => el('th', { text }))),
						...group.cookies.map((cookie) => {
							const known = key(cookie) in values
							const flags = [cookie.secure ? 'Secure' : '', cookie.httpOnly ? 'HttpOnly' : '', `SameSite=${cookie.sameSite}`].filter(Boolean).join(' ')
							return el('tr', { 'data-cookie': cookie.name },
								el('td', {}, el('code', { text: cookie.name })),
								el('td', {}, known ? el('code', { class: 'harvester-cookie-value', text: values[key(cookie)] }) : el('span', { class: 'harvester-muted', text: `•••• (${cookie.size})` }),
									el('button', { type: 'button', class: 'harvester-link', text: known ? 'Hide' : 'Show', onclick: () => {
										if (known) { delete values[key(cookie)]; render() } else send({ type: 'cookie-show', name: cookie.name, domain: cookie.domain, path: cookie.path })
									} })),
								el('td', { text: cookie.path }),
								el('td', { text: readable(cookie.expires) }),
								el('td', { text: flags }),
								el('td', {},
									el('button', { type: 'button', text: 'Edit', onclick: () => edit(cookie) }),
									el('button', { type: 'button', class: 'error', text: 'Delete', onclick: () => send({ type: 'cookie-delete', name: cookie.name, domain: cookie.domain, path: cookie.path, all: everything }) })))
						})))
			}))
		}

		const actions = el('div', { class: 'harvester-toolbar' }, scope,
			el('button', { type: 'button', text: 'Refresh', onclick: load }),
			el('button', { type: 'button', text: 'Add', onclick: () => edit(null) }))
		if (intoAccount) {
			actions.append(el('button', {
				type: 'button',
				text: 'Into the account',
				title: 'These cookies (this site’s, or all) as a cookies.txt into the job’s account: stored once you save it',
				onclick: () => send({ type: 'cookies-export', all: everything }),
			}))
		}
		if (keep) {
			actions.append(el('button', {
				type: 'button',
				text: 'Use for this job',
				title: 'Keep the browser’s session (cookies, local storage) for the job’s next run; the browser stays open',
				onclick: () => send({ type: 'cookies-keep' }),
			}))
		}
		const box = el('div', { class: 'harvester-cookies-panel', hidden: '' },
			el('h3', { text: 'Cookies' }),
			el('p', { class: 'harvester-muted', text: 'The browser’s cookies, changed at once. Values stay hidden until you show one: they are often the sign-in itself.' }),
			actions, error, status, list, form)
		const button = el('button', {
			type: 'button',
			text: 'Cookies',
			title: 'See, change or take over the browser’s cookies',
			'aria-expanded': 'false',
			onclick: () => {
				box.hidden = !box.hidden
				button.setAttribute('aria-expanded', String(!box.hidden))
				if (!box.hidden) load()
			},
		})

		function handle(data) {
			if (data.type === 'cookies') {
				host = data.host
				groups = data.groups
				error.textContent = ''
				render()
			} else if (data.type === 'cookie-value') {
				values[key(data)] = data.value
				render()
			} else if (data.type === 'cookies-error') error.textContent = data.message
			else if (data.type === 'cookies-export') {
				if (intoAccount) {
					Promise.resolve(intoAccount(data.text, data.count))
						.then((text) => { status.textContent = text || '' })
						.catch((e) => { error.textContent = e.message })
				}
			} else if (data.type === 'cookies-kept') status.textContent = 'Kept for the job: its next run starts with this session.'
			else return false
			return true
		}
		return { button, box, handle }
	}

	function networkLine() {
		const line = el('p', { class: 'harvester-muted harvester-network', text: 'Checking which address sites see…' })
		line.show = (data) => {
			const where = data.proxied ? `through ${data.via}` : 'directly, from the server'
			line.textContent = data.error
				? `Sites see: unknown (${where}; the check failed: ${data.error})`
				: `Sites see ${data.ip}${data.country ? ` (${data.country})` : ''}, ${where}.`
			line.classList.toggle('harvester-proxied', Boolean(data.proxied && !data.error))
		}
		return line
	}

	// What a job's browser showed when a step failed or a page asked for a person: the picture,
	// the page's address and title; from here the browser opens live at that page.
	async function showShot(root, job) {
		const view = el('div', { class: 'harvester-shot' }, el('p', { class: 'harvester-muted', text: 'Loading…' }))
		root.replaceChildren(view)
		let shot
		try {
			shot = await api('GET', `/jobs/${job.id}/shot`)
		} catch (e) {
			view.replaceChildren(el('p', { class: 'harvester-error', text: e.message }),
				el('button', { text: 'Back', onclick: () => show(root) }))
			return
		}
		if (!root.contains(view)) return
		view.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: `${job.name}: what the browser saw` }),
					el('p', { class: 'harvester-muted', text: `${shot.what} · ${when(shot.at)}` })),
				el('div', { class: 'harvester-actions' },
					el('button', { class: 'primary', text: 'Open in the browser', title: 'The job’s browser at this page, with its session, to look live', onclick: () => openSession(root, job, 'shot') }),
					el('button', { text: 'Back', onclick: () => show(root) }))),
			el('p', {}, el('strong', { text: shot.title || '(no title)' }), el('br'), el('code', { text: shot.url })),
			el('img', { class: 'harvester-shot-image', src: `data:image/jpeg;base64,${shot.image}`, alt: `The page ${shot.url} as the job’s browser showed it` }))
	}

	// The remote browser's navigation: Back, Forward and Reload, buttons as any other (Tab and
	// Enter or Space reach them); the server records none of them outside the mode record.
	function navigation(send) {
		return [['back', 'Back', 'The page before'], ['forward', 'Forward', 'The page after'], ['reload', 'Reload', 'Load this page again']]
			.map(([type, text, title]) => el('button', { type: 'button', text, title, onclick: () => send({ type }) }))
	}

	// A job's browser with the job's session: at the page that asked for a person (the person
	// answers it), or with at 'shot' at the page of its last failed step (to look live). "Done"
	// keeps the session and runs the job again.
	function openSession(root, job, at = '') {
		const socket = new WebSocket(`${RECORDER}?job=${job.id}${at ? `&at=${encodeURIComponent(at)}` : ''}`)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The job’s browser' })
		const url = el('input', { class: 'harvester-url', value: job.challenge_url || '', 'aria-label': 'Address' })
		url.addEventListener('keydown', (event) => { if (event.key === 'Enter') send({ type: 'goto', url: url.value }) })
		const message = el('p', { class: 'harvester-muted', text: 'Starting the browser…' })
		const network = networkLine()
		const done = el('button', {
			class: 'primary',
			text: 'Done',
			title: 'Keep this session for the job and run it again',
			onclick: () => { done.disabled = true; send({ type: 'save-session' }) },
		})
		// The job's account takes cookies when its source has the field; stored only once the
		// person confirms (there is no form here to save it with).
		const source = sources.find((s) => s.name === job.source)
		const intoAccount = source && source.credentials.includes('cookies')
			? async (text, count) => {
				if (!confirm(`Store these ${count} cookies as the account’s Cookies of “${job.name}”? They replace the ones stored, and the job’s kept session is forgotten.`)) return 'Not stored.'
				await api('PUT', `/jobs/${job.id}/credentials`, { cookies: text })
				return `${count} cookies stored in the job’s account.`
			}
			: null
		const cookies = cookiesPanel(send, intoAccount, true)
		socket.addEventListener('message', (event) => {
			const data = JSON.parse(event.data)
			if (cookies.handle(data)) return
			if (data.type === 'frame') {
				if (left(screen, socket)) return
				screen.frame(data.data)
				if (message.textContent === 'Starting the browser…') message.textContent = ''
			} else if (data.type === 'cursor') screen.style.cursor = data.cursor
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') { message.textContent = data.message; done.disabled = false; screen.say(data.message) }
			else if (data.type === 'session-saved') { socket.close(); show(root) }
			else if (data.type === 'viewport') screen.resized(data)
			else if (data.type === 'network') network.show(data)
		})
		socket.addEventListener('close', () => { if (root.contains(screen)) { message.textContent ||= 'The browser closed.'; screen.say(message.textContent) } })
		const box = wireScreen(screen, send)
		const container = el('div', { class: 'harvester-recorder' })
		container.append(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: `${job.name}: ${at === 'shot' ? 'in the job’s browser' : 'continue in the browser'}` }),
					el('p', {
						class: 'harvester-muted',
						text: at === 'shot'
							? 'The job’s browser with its session, at the page where it stopped. Look around (nothing is recorded); Done keeps this session and runs the job again, Cancel leaves it as it was.'
							: 'The site asks for a person. Answer it here as you would in your own browser (and sign in, if it asks); then press Done. The job keeps this session and runs again.',
					}))),
			overlay(
				el('div', { class: 'harvester-toolbar' }, ...navigation(send), url,
					el('button', { type: 'button', text: 'Go', onclick: () => send({ type: 'goto', url: url.value }) }),
					fullWindow(container, box, send), cookies.button, done,
					el('button', { text: 'Cancel', onclick: () => { socket.close(); show(root) } })),
				network, message, cookies.box),
			el('div', { class: 'harvester-stage' }, box))
		root.replaceChildren(container)
		screen.focusKeys()
	}

	// The recorder: the browser's screen, the person's mouse and keys to it, and the recipe so far;
	// settings() gives the form's settings the recipe's test uses (the file names, the pages).
	function openRecorder(root, page, textarea, apply, settings = () => ({})) {
		let recipe = {}
		try {
			recipe = JSON.parse(textarea.value || '{}')
		} catch (e) {}
		let proposals = {}
		let testing = false
		let tested = null
		const suggestions = {}
		// The form's "HTTP/1.1 only", known before the browser starts.
		const http1 = page.querySelector('[data-option="http1"]')
		const home = page.querySelector('[data-option="home"]')
		const query = [http1 && http1.checked ? 'http1=1' : '', home && home.checked ? 'home=1' : ''].filter(Boolean).join('&')
		const socket = new WebSocket(query ? `${RECORDER}?${query}` : RECORDER)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The recorder’s browser' })
		const url = el('input', { class: 'harvester-url', placeholder: 'https://… the site’s first page', 'aria-label': 'Address' })
		const message = el('p', { class: 'harvester-error' })
		const network = networkLine()
		const panel = el('div', { class: 'harvester-steps' })
		const modes = el('div', { class: 'harvester-modes' })
		// No job yet (or not saved): the cookies go into the form's account field, stored with Save.
		const cookieField = page.querySelector('[data-credential="cookies"]')
		const cookies = cookiesPanel(send, cookieField
			? (text, count) => {
				cookieField.value = text
				cookieField.dispatchEvent(new Event('input', { bubbles: true }))
				return `${count} cookies in the account’s Cookies field: stored with the job when you save it.`
			}
			: null, false)
		const help = {
			record: 'Use the site as usual: sign in and go to the listing. Clicks, typing, addresses typed and Back or Forward are recorded; scrolling is not (add “scroll to load more” for a listing that grows as it is scrolled).',
			browse: 'Go anywhere without recording (into a year, an issue, …) to pick there.',
			follow: 'Click one link that leads to a further listing (a year, a category): similar links are picked with it.',
			links: 'Click one file link: the similar ones are picked with it, and a pattern is proposed.',
			next: 'Click the link to the next page of this listing (if there is one); for numbered pages (1 2 3 …), click the number of the next page.',
		}
		const modeHelp = el('small', { text: help.record })
		// Where the person acts: the page, or a frame in it (recorder.js reports it as the mouse
		// or the focus comes in).
		const ordinal = (n) => `${n}${n % 10 === 1 && n % 100 !== 11 ? 'st' : n % 10 === 2 && n % 100 !== 12 ? 'nd' : n % 10 === 3 && n % 100 !== 13 ? 'rd' : 'th'}`
		const where = el('small', { class: 'harvester-where', 'aria-live': 'polite', text: 'Acting in: the page' })
		const showWhere = (frame) => {
			where.textContent = frame ? `Acting in a frame: ${frame.host || '(no address)'} (${ordinal(frame.level)} level)` : 'Acting in: the page'
		}
		const scroll = el('button', {
			type: 'button',
			text: 'Add: scroll to load more',
			title: 'A step: scroll to the end of the page until no more comes (a listing that loads more as it is scrolled)',
			onclick: () => send({ type: 'scroll' }),
		})
		const radios = {}
		const labels = [['record', 'Record steps'], ['browse', 'Browse'], ['follow', 'Pick links to follow'], ['links', 'Pick file links'], ['next', 'Pick next-page link']]
		const choose = (mode, text, level) => {
			radios[mode].checked = true
			send(level === undefined ? { type: 'mode', mode } : { type: 'mode', mode, level })
			modeHelp.textContent = text || help[mode]
			scroll.disabled = mode !== 'record'
			screen.focusKeys()
		}
		for (const [mode, label] of labels) {
			const radio = el('input', { type: 'radio', name: 'harvester-mode', value: mode })
			radio.checked = mode === 'record'
			radio.addEventListener('change', () => choose(mode))
			radios[mode] = radio
			modes.append(el('label', {}, radio, ` ${label} `))
		}
		// Pick again: the pick's mode; a level's links replace that level's, the later ones stay.
		const repick = (target, level) => {
			if (target === 'follow') {
				const where = level === 0 ? 'the listing' : `a page of level ${level}`
				choose('follow', `Go to ${where} (Browse) and click one of the links to follow: they replace level ${level + 1}’s.`, level)
			} else choose(target)
		}
		const test = el('button', {
			type: 'button',
			text: 'Test the recipe',
			title: 'Run the recipe as the job would, here, without downloading: the pages, the file links and their names',
			onclick: () => {
				if (testing) send({ type: 'test-stop' })
				else send({ type: 'test', recipe, settings: settings() })
			},
		})

		// A step of the list, with its switch "optional" (best effort: the job goes on without it)
		// and, for a click that looks like a consent banner's, the hint to make it so. The switch
		// changes the page's recipe (the form's steps from before) and tells the recorder (its own).
		function renderStep(step, index) {
			const toggle = (optional) => {
				if (optional) step.optional = true
				else delete step.optional
				send({ type: 'optional', step: index, optional })
				renderPanel()
			}
			const box = el('input', { type: 'checkbox', class: 'harvester-optional' })
			box.checked = Boolean(step.optional)
			box.addEventListener('change', () => toggle(box.checked))
			return el('li', { class: step.frame && step.frame.length ? 'harvester-framed' : '' },
				el('span', { text: `${describe(step)} ` }),
				el('label', { title: 'Best effort: when it fails (not there, not visible), the job waits a few seconds at most and goes on without it' }, box, ' optional'),
				!step.optional && looksLikeConsent(step)
					? el('button', { type: 'button', class: 'harvester-link harvester-hint', text: 'make optional?', title: 'A consent banner often shows only on the first visit', onclick: () => toggle(true) })
					: '')
		}

		function renderPanel() {
			test.textContent = testing ? 'Stop the test' : 'Test the recipe'
			panel.replaceChildren(
				el('div', { class: 'harvester-level-head' }, el('h3', { text: 'Steps' })),
				el('ol', {}, ...(recipe.steps || []).map((step, index) => renderStep(step, index))),
				el('div', { class: 'harvester-level-head' }, el('h3', { text: 'Listing' }),
					el('button', { type: 'button', text: 'Undo pick', title: 'Take back the last change of the picks', onclick: () => send({ type: 'undo-pick' }) })),
				el('p', { text: recipe.start || '(the page where you pick)' }),
				...(recipe.follow || []).map((spec, level) =>
					renderLevel(`Level ${level + 1}: links to follow`, spec, proposals[`follow${level}`], send, 'follow', level, suggestions, repick)),
				recipe.links && recipe.links.selector
					? renderLevel('File links', recipe.links, proposals.links, send, 'links', 0, suggestions, repick)
					: el('p', { text: 'File links: not picked yet.' }),
				recipe.next && recipe.next.selector
					? renderLevel('Next page', recipe.next, proposals.next, send, 'next', Number(recipe.next.level || 0), suggestions, repick)
					: el('p', { text: 'Next page: none.' }),
				el('div', { class: 'harvester-level-head' }, test),
				testing ? el('p', { class: 'harvester-muted', 'aria-live': 'polite', text: 'Testing: the browser shows the job’s way through the site…' }) : '',
				tested && !testing ? renderTest(tested) : '')
		}

		function close(keep) {
			socket.close()
			if (keep) {
				textarea.value = JSON.stringify(recipe, null, 2)
				apply(suggestions)
			}
			root.replaceChildren(page)
		}

		// The recorder starts with the form's picks, to change or remove them there.
		socket.addEventListener('open', () => send({ type: 'load', recipe }))
		socket.addEventListener('message', (event) => {
			const data = JSON.parse(event.data)
			if (cookies.handle(data)) return
			if (data.type === 'frame') { if (!left(screen, socket)) screen.frame(data.data) }
			else if (data.type === 'cursor') screen.style.cursor = data.cursor
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') { message.textContent = data.message; screen.say(data.message) }
			else if (data.type === 'viewport') screen.resized(data)
			else if (data.type === 'network') network.show(data)
			else if (data.type === 'test-started') { testing = true; tested = null; renderPanel() }
			else if (data.type === 'test') { testing = false; tested = data.result; renderPanel() }
			else if (data.type === 'where') showWhere(data.frame)
			else if (data.type === 'recipe') {
				// The picks are the recorder's (it started with the form's); the steps an earlier
				// recording made stay until new ones are recorded.
				recipe = { ...recipe, ...data.recipe, steps: data.recipe.steps.length ? data.recipe.steps : recipe.steps || [] }
				if (!data.recipe.next) delete recipe.next
				proposals = data.proposals
				message.textContent = ''
				renderPanel()
			}
		})
		socket.addEventListener('close', () => { message.textContent ||= 'The recorder’s browser closed.'; screen.say(message.textContent) })

		const box = wireScreen(screen, send)
		const container = el('div', { class: 'harvester-recorder' })
		const drawer = el('button', {
			class: 'harvester-drawer-switch',
			text: 'Recipe',
			title: 'Show or hide the steps and picks (in a full window)',
			onclick: () => container.classList.toggle('harvester-drawer-open'),
		})
		container.append(
			el('h2', { text: 'Recorder' }),
			el('p', { text: 'Sign in and go to the listing as you would (passwords are not recorded: the job uses its account). Then pick the links to follow (if the files are a level deeper) and the file links: their patterns are proposed, the matches outlined. Test the recipe to see what the job would find.' }),
			overlay(
				el('div', { class: 'harvester-toolbar' },
					...navigation(send),
					url,
					el('button', { type: 'button', text: 'Go', onclick: () => send({ type: 'goto', url: url.value }) }),
					el('button', { type: 'button', text: 'Undo step', onclick: () => send({ type: 'undo' }) }),
					el('button', { type: 'button', text: 'Clear picks', onclick: () => { recipe = { steps: recipe.steps }; send({ type: 'clear' }) } }),
					fullWindow(container, box, send),
					cookies.button,
					drawer,
					el('button', { class: 'primary', text: 'Done', onclick: () => close(true) }),
					el('button', { text: 'Cancel', onclick: () => close(false) })),
				modes, el('div', { class: 'harvester-toolbar' }, modeHelp, scroll), where, network, message, cookies.box),
			el('div', { class: 'harvester-stage' }, box, panel))
		root.replaceChildren(container)
		renderPanel()
		url.addEventListener('keydown', (event) => { if (event.key === 'Enter') send({ type: 'goto', url: url.value }) })
		url.focus()
	}

	// Values to pick for a field that takes others too, all of them as buttons below it (a value,
	// or { value, label } shown by its label). Not a datalist: browsers list only its entries
	// that match what the field holds, so a field filled with one of them offered that one alone.
	function suggestionChips(input, suggestions) {
		const chips = suggestions.map((suggestion) => {
			const { value, label } = typeof suggestion === 'object' ? suggestion : { value: String(suggestion), label: '' }
			const chip = el('button', {
				type: 'button',
				class: 'harvester-chip',
				text: label || (value === '' ? '(empty)' : value),
				title: value,
				onclick: () => {
					input.value = value
					// The field's checks and readable size follow, as when typed.
					input.dispatchEvent(new Event('input', { bubbles: true }))
					input.focus()
				},
			})
			chip.dataset.value = value
			return chip
		})
		// The chip of the value the field holds is marked.
		const mark = () => chips.forEach((chip) => chip.setAttribute('aria-pressed', String(chip.dataset.value === String(input.value))))
		input.addEventListener('input', mark)
		mark()
		return el('div', { class: 'harvester-values harvester-suggestions' }, el('small', { class: 'harvester-muted', text: 'Pick: ' }), ...chips)
	}

	// The input of an option, after its kind: an address, a pattern or a template (monospace), a
	// number with its bounds, text.
	function optionInput(option, current) {
		const number = typeof option.default === 'number'
		const attributes = { value: current, type: option.kind === 'url' ? 'url' : number ? 'number' : 'text', spellcheck: 'false' }
		if (option.placeholder) attributes.placeholder = option.placeholder
		if (number && option.minimum !== null && option.minimum !== undefined) attributes.min = String(option.minimum)
		if (number && option.maximum !== null && option.maximum !== undefined) attributes.max = String(option.maximum)
		if (['regex', 'template'].includes(option.kind)) attributes.class = 'harvester-code'
		return el('input', attributes)
	}

	// What an option shows below its input: a pattern's check as typed, a template's values to
	// insert at the caret, an address's check.
	function optionHelp(option, input) {
		if (option.kind === 'regex') {
			const verdict = el('small', { class: 'harvester-check-result' })
			const check = () => {
				if (!input.value) {
					verdict.textContent = ''
					return
				}
				try {
					// Python's named groups (?P<name>) as JavaScript writes them.
					new RegExp(input.value.replace(/\(\?P</g, '(?<'))
					verdict.textContent = '✓ a valid pattern'
					verdict.classList.remove('harvester-error')
				} catch (e) {
					verdict.textContent = `✗ ${e.message.replace(/^Invalid regular expression: /, '')}`
					verdict.classList.add('harvester-error')
				}
			}
			input.addEventListener('input', check)
			check()
			return verdict
		}
		if (option.kind === 'url') {
			const verdict = el('small', { class: 'harvester-check-result' })
			const check = () => {
				const bad = input.value && !/^https?:\/\/[^/\s]+/.test(input.value)
				verdict.textContent = bad ? '✗ an address starting with https://' : ''
				verdict.classList.toggle('harvester-error', Boolean(bad))
			}
			input.addEventListener('input', check)
			check()
			return verdict
		}
		if (option.kind === 'template' && option.values && option.values.length) {
			const insert = (name) => {
				const at = input.selectionStart ?? input.value.length
				const end = input.selectionEnd ?? at
				input.value = `${input.value.slice(0, at)}{${name}}${input.value.slice(end)}`
				input.focus()
				input.setSelectionRange(at + name.length + 2, at + name.length + 2)
			}
			return el('div', { class: 'harvester-values' }, el('small', { class: 'harvester-muted', text: 'Insert: ' }),
				...option.values.map((name) => el('button', { type: 'button', class: 'harvester-chip', text: `{${name}}`, onclick: () => insert(name) })))
		}
		return null
	}

	// The job's form; with sent (an address from ?fetch=), the new job fetches it once saved.
	function renderForm(root, job, prefill, sent) {
		const value = job || prefill || BLANK
		// The job saved so far: a new one is, after its first Save, saved again with PUT.
		let current = job
		// The zone its schedule runs in: a job keeps its own, a new one takes the person's.
		const zone = job ? job.timezone || 'UTC' : ZONE
		const name = el('input', { value: value.name, required: '' })
		const target = el('input', { value: value.target })
		// An on-demand job keeps a schedule to go back to in the field, unused.
		const schedule = el('input', { value: value.schedule || BLANK.schedule })
		// The schedule in words and its next runs in the person's time, as it is typed (the
		// server reads it as the executor does).
		const scheduleHint = el('p', { class: 'harvester-schedule', 'aria-live': 'polite' })
		let scheduleTimer
		const explainSchedule = () => {
			clearTimeout(scheduleTimer)
			scheduleTimer = setTimeout(async () => {
				try {
					const answer = await api('GET', `/schedule?expression=${encodeURIComponent(schedule.value.trim())}&timezone=${encodeURIComponent(zone)}`)
					scheduleHint.classList.toggle('harvester-error', Boolean(answer.error))
					if (answer.error) {
						scheduleHint.textContent = answer.error
						return
					}
					const format = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
					const next = answer.next.map((at) => new Date(at * 1000).toLocaleString(undefined, format)).join(' · ')
					scheduleHint.replaceChildren(el('strong', { text: answer.description }), el('br'),
						el('span', { class: 'harvester-muted', text: `Next runs (your time): ${next}` }))
				} catch (e) {
					scheduleHint.textContent = ''
				}
			}, 250)
		}
		schedule.addEventListener('input', explainSchedule)
		explainSchedule()
		const tags = el('input', { value: value.tags.join(', ') })
		const enabled = el('input', { type: 'checkbox' })
		enabled.checked = value.enabled !== false
		// On demand (sources with an address): no schedule, and maybe deleted after its run.
		const onDemandBox = el('input', { type: 'checkbox', 'data-field': 'on-demand' })
		onDemandBox.checked = !value.schedule
		const once = el('input', { type: 'checkbox', 'data-field': 'once' })
		once.checked = Boolean(value.once)
		const options = el('fieldset', {})
		const credentials = el('div', { class: 'harvester-credentials' })
		const error = el('p', { class: 'harvester-error' })
		const chosen = sources.find((s) => s.name === value.source) || sources[0]
		const optionInputs = {}
		const demanded = () => Boolean(chosen.address) && onDemandBox.checked
		let page

		// The recorder's proposals, when the person chose to use them.
		function applySuggestions(suggestions) {
			if (suggestions.path && optionInputs.path) optionInputs.path.value = suggestions.path
			if (suggestions.tags) tags.value = [...new Set([...tags.value.split(',').map((t) => t.trim()).filter(Boolean), ...suggestions.tags])].join(', ')
		}

		options.append(el('legend', { text: 'How the files are fetched' }))
		for (const [key, option] of Object.entries(chosen.options)) {
			const current = key in value.options ? value.options[key] : option.default
			if (option.kind === 'recipe') {
				const textarea = el('textarea', { rows: '10', class: 'harvester-recipe' })
				textarea.value = current
				optionInputs[key] = textarea
				options.append(field(option.title, textarea, option.description),
					el('button', {
						type: 'button',
						text: 'Record in the browser',
						onclick: () => openRecorder(root, page, textarea, applySuggestions, () => Object.fromEntries(
							['path', 'download_url', 'max_pages', 'browser'].filter((name) => optionInputs[name]).map((name) => [name, optionInputs[name].value]))),
					}))
			} else {
				if (option.kind === 'flag') {
					// On or off, kept as 1 or 0 (as the jobs saved before the checkbox).
					const box = el('input', { type: 'checkbox', 'data-option': key })
					box.checked = Number(current) === 1
					optionInputs[key] = { get value() { return box.checked ? 1 : 0 } }
					options.append(el('label', { class: 'harvester-check' }, box,
						el('span', {}, el('strong', { text: option.title }), el('br'), el('small', { class: 'harvester-muted', text: option.description }))))
					continue
				}
				if (option.choices && option.choices.length) {
					// The values it may have; several (kept comma-separated) where it allows them.
					const chosen = String(current).split(',').map((v) => v.trim())
					const select = el('select', option.multiple ? { multiple: '', size: String(Math.min(option.choices.length, 7)) } : {},
						...option.choices.map((choice) => {
							const item = el('option', { value: choice, text: choice })
							item.selected = chosen.includes(choice)
							return item
						}))
					optionInputs[key] = {
						get value() { return [...select.selectedOptions].map((item) => item.value).join(',') || option.default },
					}
					options.append(field(option.title, select, option.description))
					continue
				}
				optionInputs[key] = optionInput(option, current)
				options.append(field(option.title, optionInputs[key], option.description))
				const extra = optionHelp(option, optionInputs[key])
				if (extra) optionInputs[key].after(extra)
				if (option.suggestions && option.suggestions.length) (extra || optionInputs[key]).after(suggestionChips(optionInputs[key], option.suggestions))
				if (option.unit) {
					// A size in readable units beside the number, as it is typed.
					const readable = el('small', { class: 'harvester-size' })
					const update = () => { readable.textContent = sizeHint(optionInputs[key].value, option.unit) }
					optionInputs[key].addEventListener('input', update)
					optionInputs[key].after(readable)
					update()
				}
			}
		}

		const save = el('button', { class: 'primary', type: 'submit', form: 'harvester-job-form', text: 'Save' })
		const heading = el('span', { text: job ? `Edit ${job.name}` : `New job: ${chosen.title}` })
		const form = el('form', {
			id: 'harvester-job-form',
			class: 'harvester-form',
			onsubmit: async (event) => {
				event.preventDefault()
				if (save.disabled) return
				const body = {
					name: name.value,
					source: chosen.name,
					target: target.value,
					schedule: demanded() ? '' : schedule.value,
					once: demanded() && once.checked,
					timezone: zone,
					tags: tags.value.split(','),
					enabled: enabled.checked,
					options: Object.fromEntries(Object.entries(optionInputs).map(([key, input]) =>
						[key, typeof chosen.options[key].default === 'number' ? Number(input.value) : input.value])),
				}
				save.disabled = true
				error.textContent = ''
				try {
					current = await api(current ? 'PUT' : 'POST', current ? `/jobs/${current.id}` : '/jobs', body)
					heading.textContent = `Edit ${current.name}`
				} catch (e) {
					error.textContent = e.message
					save.disabled = false
					return
				}
				// The account typed with it is saved too, never left behind; if that fails, the form
				// stays with what was typed, and Save tries again.
				try {
					await credentials.save(current)
				} catch (e) {
					error.textContent = `The job is saved, its account is not: ${e.message}`
					save.disabled = false
					return
				}
				// Made from an address sent to Harvester: it fetches that address now.
				if (sent && onDemand(current)) {
					try {
						await sendTo(current, sent)
					} catch (e) {
						notices[current.id] = `The address is not fetched: ${e.message}`
					}
				}
				show(root)
			},
		},
		el('h2', { class: 'harvester-with-icon' }, icon(chosen.name), heading),
		field('Name', name),
		field('Folder', target, 'In your files, e.g. /Magazines/iX.'),
		chosen.address ? el('label', { class: 'harvester-check' }, onDemandBox,
			el('span', {}, el('strong', { text: 'On demand' }), el('br'),
				el('small', { class: 'harvester-muted', text: 'No schedule: it runs when you send it an address (Fetch a URL…, or Send to Harvester from any page), in place of its own, which may stay empty.' }))) : '',
		el('div', { class: 'harvester-scheduled' },
			field('Schedule', schedule, `Cron: minute hour day month weekday, in ${zone === ZONE ? 'your time zone' : 'the job’s time zone'} (${zone}). 0 6 * * * is daily at 06:00.`),
			scheduleHint),
		el('label', { class: 'harvester-check harvester-once' }, once,
			el('span', { text: 'Delete the job after a run that went through (its files stay; one that failed keeps it, to say why)' })),
		field('Tags', tags, 'Comma-separated; may use the link’s values, e.g. {year}. A tag whose value a file lacks is left out.'),
		el('label', { class: 'harvester-check harvester-scheduled' }, enabled, el('span', { text: 'Enabled: runs on its schedule' })),
		options)
		// A schedule's fields, or (on demand) whether it goes after its run.
		const showMode = () => {
			for (const part of form.querySelectorAll('.harvester-scheduled')) part.hidden = demanded()
			form.querySelector('.harvester-once').hidden = !demanded()
		}
		onDemandBox.addEventListener('change', showMode)
		showMode()
		// Below the field, now that it is in its label.
		schedule.after(suggestionChips(schedule, SCHEDULES))
		// Save and Back below the account, which Save stores with the job; Enter in the account's
		// one-line fields saves as it does in the form's (the wizard's fields answer the wizard).
		credentials.addEventListener('keydown', (event) => {
			if (event.key === 'Enter' && event.target.matches('input') && !event.target.closest('.harvester-wizard')) {
				event.preventDefault()
				form.requestSubmit(save)
			}
		})
		const back = el('button', {
			type: 'button',
			text: 'Back to the jobs',
			onclick: () => {
				if (!credentials.typed() || confirm('Leave without saving the account you typed?')) show(root)
			},
		})
		page = el('div', {}, form, credentials, el('div', { class: 'harvester-form-actions' }, error, save, back))
		root.replaceChildren(page)
		name.focus()
		renderCredentials(credentials, job, chosen)
	}

	async function show(root) {
		try {
			await renderJobs(root)
		} catch (e) {
			root.replaceChildren(el('p', { class: 'harvester-error', text: `Harvester: ${e.message}` }))
		}
	}

	// Nextcloud's look: its colours, radii and spacing variables, its buttons and inputs.
	document.head.append(el('style', {}, `
		#harvester {
			flex: 1 1 auto; min-width: 0; overflow: auto;
			background: var(--color-main-background); color: var(--color-main-text);
			border-radius: var(--body-container-radius, var(--border-radius-large));
			padding: calc(var(--default-grid-baseline, 4px) * 6) calc(var(--default-grid-baseline, 4px) * 8);
		}
		#harvester h2 { margin: 0 0 4px; font-size: 1.5em; }
		#harvester h3 { margin: 16px 0 8px; font-size: 1.15em; }
		#harvester .harvester-muted { color: var(--color-text-maxcontrast); }
		#harvester .harvester-steps .harvester-optional { margin: 0 2px 0 8px; min-height: 0; }
		#harvester .harvester-steps .harvester-hint { font-size: 90%; }
		#harvester .harvester-where { display: block; color: var(--color-text-maxcontrast); }
		#harvester .harvester-cookies-panel { max-height: 50vh; overflow: auto; padding: 8px; border: 1px solid var(--color-border); border-radius: var(--border-radius); background: var(--color-main-background); }
		#harvester .harvester-cookies { border-collapse: collapse; overflow-wrap: anywhere; }
		#harvester .harvester-cookies th, #harvester .harvester-cookies td { padding: 2px 6px; text-align: left; vertical-align: top; }
		#harvester .harvester-cookie-value { white-space: pre-wrap; }
		#harvester .harvester-network { margin: 4px 0; }
		#harvester .harvester-network.harvester-proxied { color: var(--color-success-text, var(--color-success)); }
		#harvester .harvester-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
		#harvester .harvester-header p { margin: 0; }
		#harvester .harvester-empty { padding: 48px 0; text-align: center; }
		#harvester table { width: 100%; border-collapse: collapse; }
		#harvester th { text-align: start; font-weight: normal; color: var(--color-text-maxcontrast); padding: 8px; border-bottom: 1px solid var(--color-border); }
		#harvester td { padding: 10px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top; white-space: normal; }
		#harvester tbody tr:hover { background: var(--color-background-hover); }
		#harvester .harvester-actions { text-align: end; }
		#harvester .harvester-actions button { margin-block-end: 4px; }
		#harvester td code { white-space: nowrap; }
		#harvester .harvester-message { max-width: 420px; overflow-wrap: anywhere; white-space: normal; }
		#harvester .harvester-actions button { margin-inline-start: 4px; }
		#harvester .harvester-chip { display: inline-block; margin: 0 4px 2px 0; padding: 0 8px; border-radius: var(--border-radius-pill, 999px); background: var(--color-background-dark); font-size: 0.9em; }
		#harvester .harvester-status { display: inline-block; padding: 0 8px; border-radius: var(--border-radius-pill, 999px); font-size: 0.9em; background: var(--color-background-dark); }
		#harvester .harvester-status-ok { background: var(--color-success); color: var(--color-success-text, #fff); }
		#harvester .harvester-status-failed, #harvester .harvester-status-auth { background: var(--color-error); color: var(--color-error-text, #fff); }
		#harvester .harvester-progress { margin-top: 24px; }
		#harvester .harvester-progress-head { display: flex; gap: 12px; align-items: center; }
		#harvester .harvester-progress-head h3 { margin: 0; }
		#harvester .harvester-log { list-style: none; margin: 8px 0 0; padding: 8px 12px; max-height: 320px; overflow: auto;
			font-family: var(--font-face-monospace, monospace); font-size: 13px;
			background: var(--color-background-dark); border-radius: var(--border-radius-large); }
		#harvester .harvester-log li { display: flex; gap: 10px; padding: 1px 0; }
		#harvester .harvester-log time { color: var(--color-text-maxcontrast); flex: none; }
		#harvester .harvester-log-job { color: var(--color-primary-element); flex: none; }
		#harvester .harvester-log-text { overflow-wrap: anywhere; }
		#harvester .harvester-log-error .harvester-log-text { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-log-done .harvester-log-text { color: var(--color-success-text, var(--color-success)); font-weight: bold; }
		#harvester .harvester-status-running { background: var(--color-primary-element); color: var(--color-primary-element-text); }
		#harvester .harvester-status-queued { background: var(--color-primary-element-light); color: var(--color-primary-element-light-text, var(--color-main-text)); }
		#harvester .harvester-status-held { background: var(--color-warning); color: var(--color-warning-text, #000); }
		#harvester .harvester-status-challenge { background: var(--color-warning); color: var(--color-warning-text, #000); }
		#harvester .harvester-field { display: flex; flex-direction: column; gap: 2px; margin: 12px 0; max-width: 700px; }
		#harvester .harvester-field > span { font-weight: bold; }
		#harvester .harvester-check { display: flex; align-items: center; gap: 8px; margin: 12px 0; }
		#harvester .harvester-field small { color: var(--color-text-maxcontrast); }
		#harvester .harvester-size { font-variant-numeric: tabular-nums; }
		#harvester .harvester-with-icon { display: flex; align-items: center; gap: 8px; }
		#harvester .harvester-icon { flex: none; color: var(--color-primary-element); }
		#harvester td.harvester-with-icon { display: table-cell; white-space: nowrap; }
		#harvester td.harvester-with-icon .harvester-icon { vertical-align: middle; margin-inline-end: 6px; }
		#harvester .harvester-code { font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-check-result { color: var(--color-success-text, var(--color-success)); }
		#harvester .harvester-check-result.harvester-error { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-values { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 4px; }
		#harvester .harvester-values .harvester-chip { border: none; min-height: 0; padding: 2px 8px; cursor: pointer; font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-values .harvester-chip[aria-pressed="true"] { background: var(--color-primary-element-light, var(--color-primary-light)); outline: 1px solid var(--color-primary-element, var(--color-primary)); }
		#harvester .harvester-values .harvester-chip:hover { background: var(--color-primary-element-light); }
		#harvester .harvester-schedule { margin: -4px 0 12px; max-width: 700px; }
		#harvester select[multiple] { height: auto; min-height: 0; padding: 4px; }
		#harvester select[multiple] option { padding: 2px 8px; border-radius: var(--border-radius); }
		#harvester .harvester-field input:not([type=checkbox]), #harvester .harvester-field select, #harvester .harvester-field textarea { width: 100%; margin: 0; }
		#harvester fieldset { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 8px 16px; margin: 16px 0; max-width: 760px; }
		#harvester legend { padding: 0 4px; font-weight: bold; }
		#harvester form > button, #harvester .harvester-form-actions button, #harvester .harvester-card button { margin: 8px 8px 0 0; }
		#harvester .harvester-form-actions { margin-top: 16px; }
		#harvester .harvester-error { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-credentials { margin-top: 24px; max-width: 760px; }
		#harvester .harvester-load { margin: -8px 0 12px; font-variant-numeric: tabular-nums; }
		#harvester .harvester-fetches { list-style: none; margin: 4px 0 0; padding: 0; max-width: 420px; overflow-wrap: anywhere; }
		#harvester .harvester-fetch { display: flex; gap: 8px; align-items: center; max-width: 760px; }
		#harvester .harvester-fetch input { flex: 1; margin: 0; }
		#harvester .harvester-send { margin-top: 24px; max-width: 760px; }
		#harvester .harvester-send summary { cursor: pointer; font-weight: bold; }
		#harvester .harvester-bookmarklet { display: inline-block; padding: 4px 12px; border: 1px dashed var(--color-primary-element); border-radius: var(--border-radius-pill, 999px); cursor: grab; }
		#harvester .harvester-sent { overflow-wrap: anywhere; }
		#harvester .harvester-send-kinds { display: flex; flex-wrap: wrap; gap: 8px; }
		#harvester [hidden] { display: none !important; }
		#harvester .harvester-wizard h3 { margin-top: 0; }
		#harvester .harvester-wizard-help { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--color-text-maxcontrast); }
		#harvester .harvester-wizard pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 8px 12px; background: var(--color-background-dark); border-radius: var(--border-radius); }
		#harvester .harvester-wizard button { margin: 8px 8px 0 0; }
		#harvester .harvester-kinds { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-bottom: 8px; }
		#harvester .harvester-kinds .harvester-card { margin: 0; max-width: none; display: flex; flex-direction: column; }
		#harvester .harvester-kinds .harvester-card p { flex: 1; }
		#harvester .harvester-card { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 12px 16px; margin: 12px 0; max-width: 760px; }
		#harvester .harvester-recipe, #harvester .harvester-pattern { font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-toolbar { display: flex; gap: 4px; margin: 8px 0; align-items: center; }
		#harvester .harvester-url { flex: 1; margin: 0; }
		#harvester .harvester-modes label { margin-inline-end: 12px; }
		#harvester .harvester-stage { display: flex; gap: 12px; align-items: flex-start; margin-top: 8px; }
		#harvester .harvester-screen-box { flex: 3; min-width: 0; max-width: calc((100vh - 260px) * 1.6); position: relative; }
		#harvester .harvester-screen {
			display: block; width: 100%; aspect-ratio: 1280 / 800;
			border: 1px solid var(--color-border); border-radius: var(--border-radius); background: var(--color-background-dark); cursor: default;
		}
		#harvester .harvester-keys { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; resize: none; pointer-events: none; border: 0; padding: 0; }
		#harvester .harvester-screen:not([src]) { visibility: hidden; }
		#harvester .harvester-screen-notice { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; padding: 24px; text-align: center; color: var(--color-text-maxcontrast); }
		#harvester .harvester-screen-notice[hidden] { display: none; }
		#harvester .harvester-screen-box:focus-within .harvester-screen { outline: 2px solid var(--color-primary-element); }
		#harvester .harvester-drawer-switch { display: none; }
		#harvester .harvester-overlay-toggle { display: none; }
		/* A full window: the screen fills it, the controls float over it, the recipe is a drawer. */
		#harvester .harvester-wide { position: fixed; inset: 0; z-index: 3000; margin: 0; padding: 0; background: var(--color-main-background); }
		#harvester .harvester-wide > h2, #harvester .harvester-wide > p, #harvester .harvester-wide .harvester-header { display: none; }
		#harvester .harvester-wide .harvester-stage { position: absolute; inset: 0; margin: 0; }
		#harvester .harvester-wide .harvester-screen-box { flex: 1; max-width: none; height: 100%; }
		#harvester .harvester-wide .harvester-screen { width: 100%; height: 100%; aspect-ratio: auto; object-fit: contain; border: 0; border-radius: 0; }
		#harvester .harvester-wide .harvester-overlay { position: absolute; top: 8px; left: 50%; transform: translateX(-50%); z-index: 2;
			width: min(1100px, calc(100% - 32px)); padding: 6px 10px; border-radius: var(--border-radius-large);
			background: color-mix(in srgb, var(--color-main-background) 88%, transparent); box-shadow: 0 2px 12px var(--color-box-shadow); }
		#harvester .harvester-wide .harvester-overlay-toggle { display: inline-block; float: inline-end; }
		#harvester .harvester-wide .harvester-overlay-hidden { width: auto; left: auto; right: 8px; transform: none; }
		#harvester .harvester-wide .harvester-overlay-hidden > :not(.harvester-overlay-toggle) { display: none; }
		#harvester .harvester-wide .harvester-drawer-switch { display: inline-block; }
		#harvester .harvester-wide .harvester-steps { display: none; position: absolute; top: 0; right: 0; bottom: 0; z-index: 1;
			width: min(380px, 90vw); max-height: none; padding: 72px 12px 12px; background: var(--color-main-background);
			box-shadow: -2px 0 12px var(--color-box-shadow); }
		#harvester .harvester-wide.harvester-drawer-open .harvester-steps { display: block; }
		#harvester .harvester-wide-switch[aria-pressed="true"] { background: var(--color-primary-element-light); }
		#harvester .harvester-steps { flex: 1; min-width: 300px; max-height: 80vh; overflow: auto; overflow-wrap: anywhere; font-size: 90%; }
		#harvester .harvester-level { border-inline-start: 3px solid var(--color-primary-element); padding-inline-start: 8px; margin: 8px 0; }
		#harvester .harvester-level-head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
		#harvester .harvester-level-head h3, #harvester .harvester-level-head h4 { margin-inline-end: auto; }
		#harvester .harvester-test-pages { padding-inline-start: 20px; }
		#harvester .harvester-shot-image { display: block; max-width: 100%; height: auto; border: 1px solid var(--color-border); border-radius: var(--border-radius); }
		#harvester .harvester-link { border: none; background: none; min-height: 0; padding: 0 0 0 8px; color: var(--color-primary-element); text-decoration: underline; cursor: pointer; }
		#harvester .harvester-value { background: var(--color-primary-element-light); border-radius: 4px; padding: 0 4px; margin-inline-end: 4px; font-family: var(--font-face-monospace, monospace); }
	`))

	document.addEventListener('DOMContentLoaded', async () => {
		const root = el('div', { id: 'harvester' })
		// AppAPI's page is an empty <div id="content"> inside Nextcloud's <main id="content">: with
		// Nextcloud's styles for #content it is a fixed box over the page, and anything next to it
		// is squeezed and covered. Harvester takes its place as the page's content area.
		const placeholder = document.querySelector('#content #content')
		if (placeholder) placeholder.replaceWith(root)
		else (document.querySelector('#content') || document.body).append(root)
		try {
			;[sources, presets] = await Promise.all([api('GET', '/sources'), api('GET', '/presets')])
		} catch (e) {
			// Without the sources no form can be drawn: say why instead of an empty page.
			root.replaceChildren(el('p', { class: 'harvester-error', text: `Harvester: ${e.message}` }))
			return
		}
		// Opened by the bookmarklet: the address is asked about once, and the page's own address
		// loses it (a reload shows the jobs).
		const sent = new URLSearchParams(location.search).get('fetch')
		if (sent) {
			const rest = new URLSearchParams(location.search)
			rest.delete('fetch')
			history.replaceState(history.state, '', `${location.pathname}${rest.toString() ? `?${rest}` : ''}${location.hash}`)
			try {
				await renderSend(root, sent)
			} catch (e) {
				root.replaceChildren(el('p', { class: 'harvester-error', text: `Harvester: ${e.message}` }))
			}
			return
		}
		show(root)
	})
})()
