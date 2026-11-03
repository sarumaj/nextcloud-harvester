// SPDX-License-Identifier: AGPL-3.0-or-later
// The app's page in Nextcloud: the user's jobs, a job's form (blank or from a template) with
// its account, the recorder (the app's browser, shown and driven from here), which proposes
// the links' patterns and shows what they match, and the addresses sent to on-demand jobs (Fetch
// a URL…, or ?fetch=<address> from the Send to Harvester bookmarklet). Plain JavaScript, no
// build: Nextcloud loads it into the page AppAPI renders for the top menu.
(function () {
	'use strict'

	// The page's texts in the person's language: Nextcloud's t() and n() with the app's
	// translations (l10n/<language>.js, which AppAPI adds to the page and which registers before
	// the page is drawn, on DOMContentLoaded), English where a language or a text has none. The
	// page shows them as text, so Nextcloud neither escapes nor sanitizes them as HTML. Without
	// Nextcloud's t() (a test's page) the English text is filled in here.
	const RAW = { escape: false, sanitize: false }
	const fill = (text, vars) => vars ? text.replace(/{(\w+)}/g, (match, key) => (key in vars ? String(vars[key]) : match)) : text
	function t(text, vars) {
		return typeof window.t === 'function' ? window.t('harvester', text, vars, undefined, RAW) : fill(text, vars)
	}
	function n(one, many, count, vars) {
		return typeof window.n === 'function'
			? window.n('harvester', one, many, count, vars, RAW)
			: fill((count === 1 ? one : many).replace(/%n/g, String(count)), vars)
	}
	// A text the server sends (a source's, an option's or a template's title and help, an error):
	// translated if the app's translations have it (scripts/l10n.py collects them), as sent if not.
	const said = (text) => (text ? t(String(text)) : text)
	// A server's text with values (the schedule in words, why a job waits, the progress log, a
	// run's outcome, the recorder's warnings): the English text, and its template (harvester.l10n:
	// {text, params} or a count's {text, plural, count, params}, a list {list, kind}), translated
	// here and filled with its values as text; a value may be a template of its own. Without a
	// template (a log line older than templates, an error) the text is shown as it came.
	function rendered(template) {
		if (template === null || typeof template !== 'object') return String(template ?? '')
		if (Array.isArray(template.list)) return joined(template.list.map(rendered), template.kind)
		const vars = {}
		for (const [name, value] of Object.entries(template.params || {})) vars[name] = rendered(value)
		return 'plural' in template ? n(template.text, template.plural, Number(template.count), vars) : t(template.text, vars)
	}
	const shown = (text, template) => (template && typeof template === 'object' ? rendered(template) : (text ?? ''))
	// A list in the person's language ("Monday and Thursday", "3 downloaded, 1 skipped"); in English
	// where the app has no translations of it (the page's words stay English there too).
	function joined(items, kind) {
		const language = t('Monday') !== 'Monday' ? document.documentElement.lang || 'en' : 'en'
		try {
			return new Intl.ListFormat(language, { type: kind === 'unit' ? 'unit' : 'conjunction' }).format(items)
		} catch (e) {
			return items.join(', ')
		}
	}
	// The proxy's name as the admin set it, or the default's words in the person's language.
	const proxyName = (name) => (!name || name === 'the proxy' ? t('the proxy') : name)

	const API = OC.generateUrl('/apps/app_api/proxy/harvester/api')
	// The recorder's WebSocket goes to HaRP directly (AppAPI's PHP proxy has none).
	const RECORDER = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${OC.webroot || ''}/exapps/harvester/ws/record`
	const VIEWPORT = { width: 1280, height: 800 }
	const BLANK = { name: '', source: 'site', target: '/Harvester', schedule: '0 6 * * *', options: {}, tags: [], enabled: true }
	// Usual schedules, offered below the job's schedule (cron, in the job's time zone); made when
	// shown, once the translations are there.
	const SCHEDULES = () => [
		{ value: '*/15 * * * *', label: t('Every 15 minutes') },
		{ value: '*/30 * * * *', label: t('Every 30 minutes') },
		{ value: '0 * * * *', label: t('Hourly') },
		{ value: '0 */6 * * *', label: t('Every 6 hours') },
		{ value: '0 6 * * *', label: t('Daily 06:00') },
		{ value: '0 6 * * 1-5', label: t('Weekdays 06:00') },
		{ value: '0 6 * * 1', label: t('Mondays 06:00') },
		{ value: '0 6 1 * *', label: t('Monthly, the 1st') },
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
		// The jobs' list has it from the server, without the options (GET /jobs?brief=1).
		if (!job.options) return String(job.address || '').trim()
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
			throw new Error(said(detail) || response.statusText)
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

	// A file for the person to keep, made here (a recipe file): the browser saves it.
	function download(name, text) {
		const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
		const link = el('a', { href: url, download: name })
		document.body.append(link)
		link.click()
		link.remove()
		setTimeout(() => URL.revokeObjectURL(url), 1000)
	}

	// The largest recipe file the server reads (recipe_file.MAX_BYTES): a larger one is no recipe.
	const RECIPE_FILE_BYTES = 256000

	// A job's form filled from a recipe file's text (an imported file, or a template of the
	// person's): the server checks it as Save would, and nothing is saved until Save.
	async function fromRecipe(root, text) {
		renderForm(root, null, await api('POST', '/recipes/import', { text }))
	}

	function when(timestamp) {
		return timestamp ? new Date(timestamp * 1000).toLocaleString() : t('never')
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
		return step.frame && step.frame.length ? t('{action} ⧉ in frame {host}', { action, host: frameHost(step) || t('(no address)') }) : action
	}

	// Clicks whose text reads like a consent banner's button: a banner shows on the first visit,
	// often not on the job's later ones, so the panel suggests making them optional (never on its own).
	const CONSENT = /^(?:ok(?:ay)?|got it|agree|i agree|accept(?: all)?(?: cookies)?|allow(?: all)?(?: cookies)?|akzeptieren|alle akzeptieren|alles akzeptieren|zustimmen|alle zulassen|einverstanden|zgadzam się|akceptuj(?:ę)?(?: wszystkie)?|accepter(?: tout)?|tout accepter)(?=$|[\s!.,])/iu
	const looksLikeConsent = (step) => step.action === 'click' && CONSENT.test(String(step.text || '').trim())

	function describeAction(step) {
		const selector = step.selector
		if (step.action === 'goto') return t('Open {address}', { address: step.url })
		if (step.action === 'scroll') return step.rounds ? t('Scroll to load more (at most {rounds} times)', { rounds: step.rounds }) : t('Scroll to load more')
		if (step.action === 'click') return step.text ? t('Click “{text}”', { text: step.text }) : t('Click {selector}', { selector })
		if (step.action === 'press') return t('Press {key} in {selector}', { key: step.key, selector })
		if (step.action === 'select') return t('Choose “{choice}” in {selector}', { choice: step.label || [].concat(step.value).join(', '), selector })
		if (step.action === 'hover') return t('Hover over {selector}', { selector })
		if (step.action === 'each') {
			if (!Array.isArray(step.values)) return t('Repeat for each option of {selector}', { selector })
			return selector ? t('Repeat for each of {values} in {selector}', { values: step.values.join(', '), selector }) : t('Repeat for each of {values}', { values: step.values.join(', ') })
		}
		if (step.action === 'popup') return t('Go on in the popup it opened')
		if (step.action === 'opener') return t('Back to the page that opened the popup (once it closed)')
		if (step.action === 'mail-code') {
			const of = [step.from ? t('from {sender}', { sender: step.from }) : '', step.subject ? t('subject {subject}', { subject: step.subject }) : ''].filter(Boolean).join(', ')
			return of ? t('Type the code from e-mail ({of}) into {selector}', { of, selector }) : t('Type the code from e-mail into {selector}', { selector })
		}
		if (step.action === 'ask-code') return t('Ask you for the code from {label} and type it into {selector}', { label: step.label, selector })
		if (step.action === 'approve') return t('Wait for your approval: {label}', { label: step.label })
		if (step.action === 'handover') return t('Hand the page over to you: {label}', { label: step.label })
		if (step.credential === 'totp') return t('Fill in the current one-time code ({selector})', { selector })
		if (step.credential) return t('Fill in the account’s {credential} ({selector})', { credential: credentialName(step.credential), selector })
		return t('Fill in “{value}” ({selector})', { value: step.value, selector })
	}

	// Fill steps whose own value sits in what looks like a password's field (#pw, [type=password],
	// a name with pass or pwd): a password written into the recipe before the recorder knew such
	// fields. The same check as the app's (recipe.PASSWORD_FIELD); the value is never shown.
	const PASSWORD_FIELD = /pass|pwd|(?<![a-z])pw(?![a-z])/i
	function leakedPasswords(recipe) {
		const steps = recipe && Array.isArray(recipe.steps) ? recipe.steps : []
		return steps.flatMap((step, index) => step && step.action === 'fill' && !step.credential && step.value
			&& PASSWORD_FIELD.test(String(step.selector || ''))
			? [t('Step {number} fills a value of its own into {selector}, which looks like a password field: mark it as the account’s password in the recorder’s Steps (and change the password if the recipe was shared).', { number: index + 1, selector: step.selector })]
			: [])
	}

	// What everyone's jobs and recordings hold now, without whose: "Runners 2/2 · Browsers 3/3
	// (2 jobs, 1 recording) · 3 queued". Empty when the app did not say.
	function statusText(status) {
		if (!status || !status.runners || !status.browsers) return ''
		const { runners, browsers } = status
		const open = browsers.jobs + browsers.recordings
		const held = [browsers.jobs ? n('%n job', '%n jobs', browsers.jobs) : '', browsers.recordings ? n('%n recording', '%n recordings', browsers.recordings) : ''].filter(Boolean).join(', ')
		const values = { busy: runners.busy, runners: runners.total, open, browsers: browsers.total, held, queued: status.queued }
		return held
			? t('Runners {busy}/{runners} · Browsers {open}/{browsers} ({held}) · {queued} queued', values)
			: t('Runners {busy}/{runners} · Browsers {open}/{browsers} · {queued} queued', values)
	}

	// known: what the caller has already (the progress log's poll: the queue's status).
	async function renderJobs(root, known = {}) {
		// Another view opened while the list was asked for (Edit, say) stays: the list is not drawn.
		const view = root.firstChild
		const [jobs, status] = await Promise.all([
			api('GET', '/jobs?brief=1'),
			known.status ? known.status : api('GET', '/status').catch(() => null),
		])
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
		// On a phone each row is a card: its cells one below the other, each with its column's name
		// (data-label), the actions on a line of their own.
		const columns = [t('Job'), t('Folder'), t('Schedule'), t('Tags'), t('Last run')]
		const table = el('table', { class: 'harvester-jobs' },
			el('thead', {}, el('tr', {}, ...columns.map((h) => el('th', { text: h })), el('th', {}, el('span', { class: 'harvester-hidden', text: t('Actions') })))))
		const body = el('tbody', {})
		for (const job of jobs) {
			const state = job.running && job.asking ? 'asking' : job.running ? (job.paused ? 'held' : 'running') : job.queued ? 'queued' : job.last_status || 'never'
			const label = { asking: t('Needs you'), running: t('Running'), held: t('Paused'), queued: t('Queued'), ok: t('OK'), failed: t('Failed'), auth: t('Sign-in refused'), challenge: t('Needs you'), changed: t('Changed'), never: t('Not run yet') }[state] || state
			body.append(el('tr', {},
				el('td', { class: 'harvester-with-icon', title: said((sources.find((x) => x.name === job.source) || {}).title) || job.source },
					icon(job.source), el('strong', { text: job.name }),
					// Not "paused": that is a running job held by Pause.
					job.enabled ? '' : el('span', { class: 'harvester-chip', text: t('off'), title: t('Not enabled: runs only with Run now') })),
				el('td', { class: 'harvester-muted', 'data-label': columns[1], text: job.target }),
				el('td', { 'data-label': columns[2] }, onDemand(job)
					? el('span', { class: 'harvester-chip harvester-on-demand', text: t('On demand'), title: t('No schedule: runs when you send it an address') })
					: el('code', { text: job.schedule }),
				job.once ? el('span', { class: 'harvester-chip', text: t('once'), title: t('Deleted after a run that went through; its files stay') }) : ''),
				el('td', { 'data-label': columns[3] }, ...job.tags.map((tag) => el('span', { class: 'harvester-chip', text: tag }))),
				el('td', { 'data-label': columns[4] },
					el('span', { class: `harvester-status harvester-status-${state}`, text: label }),
					el('div', {
						class: 'harvester-muted harvester-message',
						'data-job-message': String(job.id),
						text: job.running ? shown(job.waiting, job.waiting_l10n) || t('Starting')
							: job.queued ? shown(job.waiting, job.waiting_l10n) || t('Waiting for its site or a free runner')
								: job.last_run ? `${when(job.last_run)} · ${shown(job.last_message, job.last_message_l10n)}` : '',
					}),
					notices[job.id] ? el('div', { class: 'harvester-error harvester-message', role: 'alert', text: notices[job.id] }) : '',
					state === 'asking' ? askingBox(root, job) : '',
					fetchList(job, act)),
				el('td', { class: 'harvester-actions' },
					state === 'challenge' ? el('button', { class: 'primary', text: t('Continue in the browser'), onclick: () => openSession(root, job) }) : '',
					// The site seems to have changed: its recipe walked again, without downloads
					// (the progress log tells what each level found); or picked anew in its form.
					state === 'changed' ? el('button', { class: 'primary', text: t('Test the recipe'), title: t('Walk the recipe as a run would, downloading nothing: the progress log says what each level finds'), onclick: act(job, '/test') }) : '',
					// While it runs: pause (or resume) and stop instead of Run now; a stopped run
					// keeps what it saved, and the next run goes on from there.
					...(job.running
						? [
							el('button', { text: job.paused ? t('Resume') : t('Pause'), onclick: act(job, job.paused ? '/resume' : '/pause') }),
							el('button', { text: t('Stop'), onclick: act(job, '/stop') }),
						]
						: [onDemand(job) && !ownAddress(job) ? ''
							: job.queued
								? el('button', { text: t('Run now'), disabled: '', title: t('Queued already: it runs once its turn comes') })
								: el('button', { text: t('Run now'), onclick: act(job, '/run') })]),
					onDemand(job) ? el('button', { text: t('Fetch a URL…'), 'aria-expanded': 'false', onclick: (event) => fetchForm(event.currentTarget, job) }) : '',
					el('button', { text: t('Edit'), onclick: (event) => editJob(root, job, event.currentTarget) }),
					el('button', {
						class: 'error',
						text: t('Delete'),
						onclick: (event) => {
							if (confirm(t('Delete {name}? The files it downloaded stay.', { name: job.name }))) act(job, '', 'DELETE')(event)
						},
					}))))
		}
		// Shown once: the next drawing of the list is without them.
		for (const id of Object.keys(notices)) delete notices[id]
		table.append(body)
		// Everyone's load, so a job that waits is understood; updated by the progress log's polls.
		const load = el('p', { class: 'harvester-muted harvester-load', role: 'status', title: t('All people’s jobs and recordings'), text: statusText(status) })
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: 'Harvester' }),
					el('p', { class: 'harvester-muted', text: t('Fetches files from websites into your folders on a schedule, and tags them.') })),
				el('button', { class: 'primary', text: t('New job'), onclick: () => renderStart(root) })),
			load,
			jobs.length ? table : el('div', { class: 'harvester-empty' },
				el('p', { text: t('No jobs yet.') }),
				el('p', { class: 'harvester-muted', text: t('A job fetches the files of one site: record it in the browser, or start from a template.') })),
			jobs.length ? progressPanel(root, jobs) : '',
			sendHelp())
	}

	// A job's form with its options, which the list does not have: asked for as the form opens.
	async function editJob(root, job, button) {
		button.disabled = true
		try {
			renderForm(root, await api('GET', `/jobs/${job.id}`))
		} catch (e) {
			notices[job.id] = e.message
			await show(root)
		}
	}

	// What a running job asks its person (harvester.needs): the code from their phone, typed here
	// and sent to the run (never kept), or their approval in an app, before the time shown.
	function askingBox(root, job) {
		const asking = job.asking
		const until = new Date(asking.until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
		if (asking.kind !== 'code') {
			return el('p', { class: 'harvester-asking', text: `${t('{label}: approve it on your phone or in the app. The job goes on once the site shows it went through.', { label: asking.label })} ${t('The job waits for it until {time}.', { time: until })}` })
		}
		const input = el('input', { class: 'harvester-code', autocomplete: 'one-time-code', spellcheck: 'false', maxlength: '64', 'aria-label': t('Enter the code from {label}', { label: asking.label }) })
		const note = el('small', { class: 'harvester-muted', text: t('The job waits for it until {time}.', { time: until }) })
		const form = el('form', {
			class: 'harvester-asking',
			onsubmit: async (event) => {
				event.preventDefault()
				if (!input.value.trim()) return
				form.querySelector('button').disabled = true
				try {
					await api('POST', `/jobs/${job.id}/code`, { code: input.value.trim() })
					input.value = ''
					note.textContent = t('Sent: the job goes on.')
				} catch (e) {
					note.textContent = e.message
					form.querySelector('button').disabled = false
				}
			},
		}, el('label', {}, el('span', { text: t('Enter the code from {label}', { label: asking.label }) }), input),
		el('button', { class: 'primary', type: 'submit', text: t('Send') }), note)
		return form
	}

	// An on-demand job's waiting addresses below its message, oldest first: the one being fetched
	// (Stop ends it), the others each with Cancel.
	function fetchList(job, act) {
		if (!(job.fetches || []).length) return ''
		return el('ul', { class: 'harvester-fetches', 'aria-label': t('Addresses {name} waits to fetch', { name: job.name }) },
			...job.fetches.map((fetch) => el('li', {},
				el('code', { text: fetch.url }),
				fetch.running
					? el('span', { class: 'harvester-muted', text: ` ${t('fetching')}` })
					: el('button', { class: 'harvester-link', text: t('Cancel'), 'aria-label': t('Cancel the fetch of {address}', { address: fetch.url }), onclick: act(job, `/fetch/${fetch.id}`, 'DELETE') }))))
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
		const input = el('input', { type: 'url', required: '', placeholder: t('https://…'), 'aria-label': t('The address {name} fetches', { name: job.name }), spellcheck: 'false' })
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
		el('button', { class: 'primary', type: 'submit', text: t('Fetch') }),
		el('button', { type: 'button', text: t('Cancel'), onclick: () => fetchForm(button, job) }))
		row.after(el('tr', { class: 'harvester-fetch-row' }, el('td', { colspan: '6' }, form,
			el('small', { class: 'harvester-muted', text: t('Fetched in place of the job’s own address, for this run alone; files it fetched before are not fetched again.') }), error)))
		input.focus()
	}

	// Queues the address for the on-demand job; the error says why it was refused.
	async function sendTo(job, url) {
		const answer = await api('POST', `/jobs/${job.id}/fetch`, { url: url.trim() })
		if (!answer.new) notices[job.id] = t('That address waits to be fetched already.')
		return answer
	}

	// Send to Harvester: a bookmarklet that opens this page with the page one is on (?fetch=).
	function sendHelp() {
		const code = `javascript:void window.open('${PAGE}?fetch='+encodeURIComponent(location.href))`
		return el('details', { class: 'harvester-send' },
			el('summary', { text: t('Send to Harvester from any page') }),
			el('p', { text: t('Drag this link to your bookmarks bar. On a page with a video, gallery, feed or files, click the bookmark: Harvester opens and asks which on-demand job fetches the page (or makes a new one from it).') }),
			el('a', {
				class: 'harvester-bookmarklet',
				href: code,
				title: t('Drag it to your bookmarks bar'),
				// Nextcloud's page runs no script from a link: here it is only dragged.
				onclick: (event) => event.preventDefault(),
			}, t('Send to Harvester')))
	}

	// The page opened with ?fetch=<address> (the bookmarklet): which on-demand job fetches it, or
	// a new job of a source with an address, filled with it.
	async function renderSend(root, url) {
		const jobs = (await api('GET', '/jobs?brief=1')).filter((job) => onDemand(job) && addressOf(job.source))
		const error = el('p', { class: 'harvester-error', role: 'alert' })
		const select = el('select', { 'aria-label': t('On-demand job') },
			...jobs.map((job) => el('option', { value: String(job.id), text: `${job.name} (${job.target})` })))
		const fetchButton = el('button', {
			class: 'primary',
			text: t('Fetch'),
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
		}, icon(source.name), el('span', { text: said(source.title) })))
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {}, el('h2', { text: t('Send to Harvester') }), el('p', {}, el('code', { class: 'harvester-sent', text: url }))),
				el('button', { text: t('Back to the jobs'), onclick: () => show(root) })),
			jobs.length
				? el('div', { class: 'harvester-card' }, field(t('Fetch it with'), select, t('An on-demand job: the address takes the place of its own, for this run alone.')), fetchButton, error)
				: el('p', { class: 'harvester-muted', text: t('No on-demand job yet: make one from this address.') }),
			el('div', { class: 'harvester-card' },
				el('h3', { text: t('New job from this address') }),
				el('div', { class: 'harvester-send-kinds' }, ...kinds)))
		;(jobs.length ? fetchButton : root.querySelector('.harvester-send-kinds button')).focus()
	}

	// The jobs' progress log: what the runs do, live while one runs, of all jobs or of one.
	// Polled, often while a job runs or waits its turn (every 2 seconds, else 30); a run that is
	// queued, starts or ends redraws the list (its states and messages change). Not while the
	// page is hidden (another tab, a minimized window): polled at once when it shows again.
	// The lines shown so far, kept while the page is open: the list drawn again (a run that starts
	// or ends) shows them at once and asks only for the newer ones.
	const logKept = { job: null, after: 0, entries: [] }
	function progressPanel(root, jobs) {
		const select = el('select', { 'aria-label': t('Job') },
			el('option', { value: '', text: t('All jobs') }),
			...jobs.map((job) => el('option', { value: String(job.id), text: job.name })))
		try {
			const kept = localStorage.getItem('harvester-log-job') || ''
			if (jobs.some((job) => String(job.id) === kept)) select.value = kept
		} catch (e) { /* no storage: all jobs */ }
		const list = el('ol', { class: 'harvester-log', 'aria-live': 'polite' })
		const panel = el('section', { class: 'harvester-progress' },
			el('div', { class: 'harvester-progress-head' }, el('h3', { text: t('Progress') }), select), list)
		let after = 0
		let timer = null
		let busy = false
		const keep = (entries) => {
			logKept.job = select.value
			logKept.entries = [...logKept.entries, ...entries].slice(-300)
			logKept.after = after
		}
		const running = jobs.filter((job) => job.running).map((job) => job.id)
		const queued = jobs.filter((job) => job.queued && !job.running).map((job) => job.id)
		// The jobs whose runs wait for their person (a code): the list is drawn again when they change.
		const asking = jobs.filter((job) => job.running && job.asking).map((job) => job.id)
		// The on-demand jobs' waiting addresses, as the log's polls give them (by job, in order).
		const sorted = (fetches) => JSON.stringify(Object.keys(fetches).sort().map((id) => [id, fetches[id]]))
		const fetches = sorted(Object.fromEntries(jobs.filter((job) => (job.fetches || []).length).map((job) => [String(job.id), job.fetches.map((fetch) => fetch.id)])))
		const clock = (at) => {
			const date = new Date(at * 1000)
			return date.toDateString() === new Date().toDateString() ? date.toLocaleTimeString() : date.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })
		}
		const line = (entry) => el('li', { class: `harvester-log-${entry.level}` },
			el('time', { text: clock(entry.at) }),
			select.value ? '' : el('span', { class: 'harvester-log-job', text: entry.name }),
			el('span', { class: 'harvester-log-text', text: shown(entry.message, entry.message_l10n) },
				// The job's browser's picture of the failed step (or of the page asking for a
				// person): kept for its newest failure only, so an older line's may be gone.
				entry.shot ? el('button', {
					type: 'button',
					class: 'harvester-link',
					text: t('Show what the browser saw'),
					onclick: () => showShot(root, jobs.find((job) => job.id === entry.job) || { id: entry.job, name: entry.name }),
				}) : ''))
		async function poll() {
			if (!document.body.contains(panel)) {
				document.removeEventListener('visibilitychange', woken)
				return
			}
			clearTimeout(timer)
			timer = null
			if (document.hidden) return
			busy = true
			try {
				const data = await api('GET', `/log?after=${after}${select.value ? `&job=${select.value}` : ''}`)
				// Another view opened while the log was asked for (a job's form): it is not replaced.
				if (!document.body.contains(panel)) return
				if (data.lines.length) after = data.lines[data.lines.length - 1].id
				keep(data.lines)
				const waiting = (data.queued || []).filter((id) => !data.running.includes(id))
				if (String(data.running) !== String(running) || String(waiting) !== String(queued) || sorted(data.fetches || {}) !== fetches
					|| String(data.asking || []) !== String(asking)) {
					show(root, { status: data.status })
					return
				}
				const load = root.querySelector('.harvester-load')
				if (load && data.status) load.textContent = statusText(data.status)
				// Why a queued job waits (its place, a runner, its site), or a running one (a browser).
				for (const [id, reason] of Object.entries(data.waiting || {})) {
					const message = root.querySelector(`[data-job-message="${id}"]`)
					if (message) message.textContent = shown(reason, (data.waiting_l10n || {})[id])
				}
				const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 24
				for (const entry of data.lines) {
					list.append(line(entry))
					const message = root.querySelector(`[data-job-message="${entry.job}"]`)
					if (message && running.includes(entry.job) && !(data.waiting || {})[entry.job]) message.textContent = shown(entry.message, entry.message_l10n)
				}
				while (list.children.length > 300) list.firstChild.remove()
				if (!list.children.length) list.append(el('li', { class: 'harvester-muted', text: t('No runs yet.') }))
				else if (data.lines.length && list.firstChild.classList.contains('harvester-muted')) list.firstChild.remove()
				if (stick) list.scrollTop = list.scrollHeight
			} catch (e) { /* tried again below */ } finally { busy = false }
			timer = setTimeout(poll, running.length || queued.length ? 2000 : 30000)
		}
		// Shown again: polled at once, unless a poll is under way (it sets the next).
		function woken() {
			if (!document.hidden && !busy) poll()
		}
		document.addEventListener('visibilitychange', woken)
		select.addEventListener('change', () => {
			try { localStorage.setItem('harvester-log-job', select.value) } catch (e) { /* not kept */ }
			clearTimeout(timer)
			after = 0
			logKept.entries = []
			logKept.job = select.value
			list.replaceChildren()
			poll()
		})
		// The lines the list showed before it was drawn again, of the same job (or all).
		if (logKept.job === select.value) {
			after = logKept.after
			list.append(...logKept.entries.map(line))
			requestAnimationFrame(() => { list.scrollTop = list.scrollHeight })
		} else {
			logKept.entries = []
		}
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
				const row = field(said(spec.title), inputs[name], said(spec.description))
				if (spec.suggestions && spec.suggestions.length) inputs[name].after(suggestionChips(inputs[name], spec.suggestions))
				return row
			})
			return el('div', { class: 'harvester-card' },
				el('h3', { class: 'harvester-with-icon' }, icon(preset.source), el('span', { text: said(preset.title) })),
				el('p', { class: 'harvester-muted', text: said(preset.description) }), ...fields,
				el('button', {
					class: 'primary',
					text: t('Use this template'),
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
			el('h3', { class: 'harvester-with-icon' }, icon(source.name), el('span', { text: said(source.title) })),
			el('p', { class: 'harvester-muted', text: said(source.description) }),
			el('button', { class: 'primary', text: t('Start'), onclick: () => renderForm(root, null, { ...BLANK, source: source.name }) })))
		// A recipe file (Export recipe in a job's form): read here, checked by the server.
		const file = el('input', { type: 'file', accept: '.json,application/json', 'aria-label': t('Recipe file') })
		file.addEventListener('change', async () => {
			error.textContent = ''
			const chosenFile = file.files[0]
			if (!chosenFile) return
			if (chosenFile.size > RECIPE_FILE_BYTES) {
				error.textContent = t('The file is larger than a recipe ({size} KB at most).', { size: RECIPE_FILE_BYTES / 1000 })
				file.value = ''
				return
			}
			try {
				await fromRecipe(root, await chosenFile.text())
			} catch (e) {
				error.textContent = e.message
				file.value = ''
			}
		})
		const importCard = el('div', { class: 'harvester-card harvester-import' },
			el('h3', { text: t('Import a recipe') }),
			el('p', { class: 'harvester-muted', text: t('A recipe file (Export recipe in a job’s form): the job’s form opens filled in, to choose the folder and the account; nothing is saved until Save.') }),
			file)
		// The person's own templates (Save as template in a job's form), each to use or remove.
		const own = el('div', { class: 'harvester-own-templates' })
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {}, el('h2', { text: t('New job') }), el('p', { class: 'harvester-muted', text: t('What should the job fetch?') })),
				el('button', { text: t('Back to the jobs'), onclick: () => show(root) })),
			el('div', { class: 'harvester-kinds' }, ...kinds),
			importCard,
			own,
			cards.length ? el('h3', { text: t('Templates for known sites') }) : '',
			...cards, error)
		ownTemplates(root, own, error)
		// The button that opened this view is gone: the keyboard goes on from the first choice.
		const first = root.querySelector('.harvester-kind button')
		if (first) first.focus()
	}

	// The person's own templates on the New job page: Use opens the form filled in (checked as an
	// imported file is), Remove forgets the template.
	async function ownTemplates(root, container, error) {
		let templates = []
		try {
			templates = await api('GET', '/templates')
		} catch (e) {
			error.textContent = t('Your templates could not be read: {error}', { error: e.message })
			return
		}
		container.replaceChildren()
		if (!templates.length) return
		container.replaceChildren(el('h3', { text: t('Your templates') }), ...templates.map((template) => el('div', { class: 'harvester-card harvester-own-template' },
			el('h3', { class: 'harvester-with-icon' }, icon(template.file.source), el('span', { text: template.name })),
			el('p', { class: 'harvester-muted', text: said((sources.find((s) => s.name === template.file.source) || {}).title) || template.file.source }),
			el('button', {
				class: 'primary',
				text: t('Use this template'),
				onclick: async () => {
					error.textContent = ''
					try {
						await fromRecipe(root, JSON.stringify(template.file))
					} catch (e) {
						error.textContent = e.message
					}
				},
			}),
			el('button', {
				class: 'error',
				text: t('Remove'),
				'aria-label': t('Remove the template {name}', { name: template.name }),
				onclick: async () => {
					if (!confirm(t('Remove the template {name}?', { name: template.name }))) return
					try {
						await api('DELETE', `/templates/${template.id}`)
					} catch (e) {
						error.textContent = e.message
						return
					}
					ownTemplates(root, container, error)
				},
			}))))
	}

	// What a source's account is for, above its fields (each field says what it is used for).
	const ACCOUNT_USE = () => ({
		site: t('The recorded sign-in’s fields get the username, password and current one-time code; or a signed-in browser’s cookies instead of a sign-in.'),
		mail: t('The mailbox’s sign-in: a password, or OAuth2’s client and refresh token.'),
		remote: t('rclone’s configuration of the remote, with its passwords or tokens.'),
		releases: t('A token, for private repositories and for more requests than GitHub allows without one.'),
	})
	// A credential's name in a step's words ("Fill in the account's password").
	const credentialName = (name) => ({ username: t('username'), password: t('password') })[name] || name

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
		container.append(el('h3', { text: t('The account this job signs in with') }),
			el('p', { text: `${ACCOUNT_USE()[source.name] || t('What the job signs in with where its source asks for it.')} ${t('Stored encrypted in Nextcloud; never shown again.')}` }))
		let stored = {}
		if (job) {
			try {
				stored = await api('GET', `/jobs/${job.id}/credentials`)
			} catch (e) {
				error.textContent = t('Whether an account is stored is unknown: {error}', { error: e.message })
			}
		}
		const inputs = {}
		for (const name of source.credentials) {
			// Several lines (rclone's configuration, cookies) in a text area; secrets hidden.
			const placeholder = stored[name] ? t('(stored, leave empty to keep)') : ''
			const [title, help] = ((source.credential_help || {})[name] || [name, '']).map(said)
			inputs[name] = ['config', 'cookies', 'code_mailbox'].includes(name)
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
					text: t('Set up with rclone'),
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
				text: t('Save account'),
				onclick: async () => {
					button.disabled = true
					error.textContent = ''
					status.textContent = ''
					try {
						const done = await container.save(job)
						await renderCredentials(container, job, source, done ? t('Account saved.') : t('Nothing typed: the stored account stays.'))
					} catch (e) {
						error.textContent = e.message
						button.disabled = false
					}
				},
			})
			: el('p', { class: 'harvester-muted', text: t('Saved with the job when you press Save.') })
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
		const cancel = el('button', { type: 'button', text: t('Cancel'), onclick: close })
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
			panel.replaceChildren(el('h3', { text: t('Set up with rclone') }),
				el('p', { class: 'harvester-muted', text: t('rclone asks for the remote’s settings one at a time, as “rclone config” does.') }),
				field(t('Kind of remote'), select), error,
				el('button', { type: 'button', class: 'primary', text: t('Start'), onclick: () => step('POST', '/rclone/wizard', { type: select.value }) }),
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
					el('option', { value: example.value, text: example.help ? `${example.value || t('(empty)')}: ${example.help.split('\n')[0]}` : example.value })))
			} else if (question.type === 'bool') {
				input = el('select', {}, el('option', { value: 'true', text: t('Yes') }), el('option', { value: 'false', text: t('No') }))
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
				text: t('Back'),
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
			field(question.required ? t('{name} (required)', { name: question.name }) : question.name, input),
			suggestions,
			el('p', { class: 'harvester-wizard-help', text: question.help }),
			view.authorize
				? el('div', { class: 'harvester-wizard-authorize' },
					el('p', {},
						el('strong', { text: `${t('Sign in here:')} ` }),
						`${t('the provider’s sign-in opens in Harvester’s own browser, shown below; rclone takes the token when you are done.')} `,
						t('Some providers refuse to sign in in such a browser (Google may say “This browser or app may not be secure”): then use your own computer, as below.')),
					el('button', { type: 'button', text: t('Sign in here'), onclick: () => signInHere(view) }),
					el('p', { text: t('Or on your own computer: with a web browser and rclone (the same version is best), run this, sign in, and paste everything it prints here:') }),
					el('pre', { class: 'harvester-code', text: view.authorize }))
				: '',
			error,
			el('button', { type: 'submit', class: 'primary', text: t('Next') }), back, cancel)
			panel.replaceChildren(el('h3', { text: t('Set up with rclone: {kind}', { kind: view.type }) }), form)
			input.focus()
		}

		// The provider's sign-in in the app's browser (the recorder's screen): the server runs
		// `rclone authorize`, and its token answers the question once the person signed in.
		function signInHere(view) {
			const socket = new WebSocket(`${RECORDER}?rclone=${encodeURIComponent(session)}`)
			signIn = socket
			socket.addEventListener('close', () => { if (signIn === socket) signIn = null })
			const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
			const screen = el('img', { class: 'harvester-screen', alt: t('The sign-in’s browser') })
			const url = el('input', { class: 'harvester-url', readonly: '' })
			const starting = t('Starting rclone and the browser…')
			const message = el('p', { class: 'harvester-muted', text: starting })
			let finished = false
			const back = el('button', { type: 'button', text: t('Back to the question'), onclick: () => { finished = true; socket.close(); showQuestion(view) } })
			socket.addEventListener('message', (event) => {
				// A frame (a binary message): the screen shows it (wireScreen).
				if (typeof event.data !== 'string') {
					if (message.textContent === starting) message.textContent = ''
					return
				}
				const data = JSON.parse(event.data)
				if (data.type === 'frame') {
					if (left(screen, socket)) return
					screen.frame(data.data)
					if (message.textContent === starting) message.textContent = ''
				} else if (data.type === 'cursor') screen.style.cursor = data.cursor
				else if (data.type === 'url') url.value = data.url
				else if (data.type === 'error') { message.textContent = shown(data.message, data.message_l10n); screen.say(message.textContent) }
				else if (data.type === 'viewport') screen.resized(data)
				else if (data.type === 'rclone-done') {
					finished = true
					socket.close()
					render(data.view)
				}
			})
			socket.addEventListener('close', () => {
				if (!finished && panel.contains(screen)) {
					message.textContent ||= t('The browser closed.')
					screen.say(message.textContent)
				}
			})
			const box = wireScreen(screen, send, socket)
			const container = el('div', { class: 'harvester-recorder' })
			container.append(
				el('p', { class: 'harvester-muted', text: t('Sign in and allow rclone’s access; the browser closes by itself once rclone has the token (at most ten minutes).') }),
				overlay(el('div', { class: 'harvester-toolbar' }, url, fullWindow(container, box, send), back), message),
				el('div', { class: 'harvester-stage' }, box))
			panel.replaceChildren(el('h3', { text: t('Set up with rclone: sign in to {kind}', { kind: view.type }) }), container)
			screen.focusKeys()
		}

		function showDone(view) {
			const back = el('button', { type: 'button', text: t('Back'), onclick: () => step('POST', `/rclone/wizard/${session}/back`) })
			if (view.error) {
				panel.replaceChildren(el('h3', { text: t('Set up with rclone') }),
					el('p', { class: 'harvester-error', text: view.error }), error, back, cancel)
				return
			}
			panel.replaceChildren(el('h3', { text: t('Set up with rclone: done') }),
				el('p', { text: t('The configuration rclone wrote (passwords obscured, as rclone keeps them). Use it, then save it with the job’s Save (or Save account).') }),
				el('pre', { class: 'harvester-code harvester-wizard-result', text: view.config }),
				error,
				el('button', {
					type: 'button',
					class: 'primary',
					text: t('Use this configuration'),
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
	// page): its element (a selector, with the alternatives a job tries when it finds nothing) and
	// its pattern, both editable, what it matches on the page, examples with their values, the
	// proposed file name and tags; and the same controls for each: Pick again (repick) and Remove.
	function renderLevel(title, spec, proposal, send, target, level, suggestions, repick) {
		if (target === 'links' && spec.kind === 'json') return renderJson(title, spec, proposal, send)
		const selector = el('input', { class: 'harvester-pattern', value: spec.selector || '', spellcheck: 'false' })
		selector.addEventListener('change', () => send({ type: 'selector', target, level, selector: selector.value }))
		const box = el('div', { class: 'harvester-level', 'data-target': target, 'data-level': String(level) },
			el('div', { class: 'harvester-level-head' }, el('h4', { text: title }),
				el('button', { type: 'button', text: t('Pick again'), title: t('Pick this on the page again (in its picking mode)'), onclick: () => repick(target, level) }),
				el('button', { type: 'button', class: 'error', text: t('Remove'), title: target === 'follow' ? t('Remove this level (the levels after it move up)') : t('Remove this pick'), onclick: () => send({ type: 'remove', target, level }) })),
			field(target === 'next' && spec.kind === 'number' ? t('The numbers’ container (CSS selector)') : t('Element (selector)'), selector))
		if (spec.alternatives && spec.alternatives.length) {
			box.append(el('p', { class: 'harvester-muted', text: t('Alternatives, tried in this order when it finds nothing: {alternatives}', { alternatives: spec.alternatives.join('  ·  ') }) }))
		}
		if (target === 'next') return renderNext(box, spec, proposal, send)
		// Elements without an address (buttons, rows, cards): the job clicks them in the browser.
		const click = el('input', { type: 'checkbox' })
		click.checked = Boolean(spec.click)
		click.addEventListener('change', () => send({ type: 'click', target, level, click: click.checked }))
		box.append(el('label', { class: 'harvester-click' }, click, ` ${t('Click in the browser')}`))
		if (spec.click) {
			box.append(el('p', { class: 'harvester-muted', text: target === 'links'
				? t('These have no address: the job clicks each in the browser and keeps the file it gives (a download, or a tab that shows the file). The pattern is on their texts.')
				: t('These have no address: the job clicks each in the browser, reads the next level where it lands (a page, a new tab, a dialog) and goes back to this listing. The pattern is on their texts.') }))
		}
		// The pages the file links lead to saved as files (a receipt, an article), not downloaded.
		if (target === 'links') {
			const save = el('select', {},
				...[['', t('Files (download them)')], ['pdf', t('PDF')], ['html', t('HTML (one file)')], ['markdown', t('Markdown (the main text)')]]
					.map(([value, label]) => el('option', { value, text: label })))
			save.value = spec.save || ''
			save.addEventListener('change', () => send({ type: 'save', save: save.value }))
			box.append(field(t('Save the pages as'), save))
			if (spec.save) {
				box.append(el('p', { class: 'harvester-muted', text: t('The job opens the page each of these leads to in the browser and saves the page itself as a file, named after its title ({title}).', { title: '{title}' }) }))
			}
			if (spec.capture && spec.capture.length) {
				box.append(el('p', { class: 'harvester-muted', text: t('The job keeps the files of these types that each page loads: {types}', { types: spec.capture.join(', ') }) }))
			}
		}
		const pattern = el('input', { class: 'harvester-pattern', value: spec.pattern || '', spellcheck: 'false' })
		pattern.addEventListener('change', () => send({ type: 'pattern', target, level, pattern: pattern.value }))
		box.append(field(spec.click ? t('Pattern on their texts (named groups become values)') : t('Pattern (named groups become values)'), pattern))
		if (!proposal) return box
		if (proposal.error) box.append(el('p', { class: 'harvester-error', text: proposal.error }))
		box.append(el('p', { text: n('On this page: {matching} of %n link matches (solid outline; dashed: left out).', 'On this page: {matching} of %n links match (solid outline; dashed: left out).', proposal.found, { matching: proposal.matching }) }))
		box.append(examplesList(proposal))
		box.append(renderValues(spec, send, target, level, false),
			el('button', { type: 'button', text: t('Pick a value'), title: t('Click a text near one of these links (a date, a title in its row or card): a value for file names and tags'), onclick: () => repick(target, level, 'value') }))
		if (target === 'links' && proposal.path) {
			const use = (label, apply) => el('button', { type: 'button', text: label, onclick: (e) => { apply(); e.target.textContent = t('✔ will be used') } })
			box.append(el('p', { text: t('Proposed file name: {path}', { path: proposal.path }) }), use(t('Use it'), () => { suggestions.path = proposal.path }))
			// The same script serves all the files (download.php?id=N): the name the server gives
			// each file is likely the better one.
			if (proposal.generic) {
				box.append(el('p', { class: 'harvester-muted', text: t('The links all have the same name: the name the server gives each file ({disposition}) may be better.', { disposition: '{disposition}' }) }),
					use(t('Use {disposition}', { disposition: '{disposition}' }), () => { suggestions.path = proposal.path.replace(/[^/]*$/, '{disposition}') }))
			}
			if (proposal.tags.length) {
				box.append(el('p', { text: t('Proposed tags: {tags}', { tags: proposal.tags.join(', ') }) }), use(t('Use them'), () => { suggestions.tags = proposal.tags }))
			}
		}
		return box
	}

	// The examples of a pick: each link's text and its values (the pattern's and the page's).
	function examplesList(proposal) {
		return el('ul', {}, ...(proposal.examples || []).map((example) => el('li', {},
			el('span', { text: example.text || example.url }), ' ',
			...Object.entries(example.values).map(([k, v]) => el('span', { class: 'harvester-value', text: `${k}=${v}` })))))
	}

	// Where a value of the page is read, in words: in the link, or in its row or card.
	function valueWhere(value) {
		const where = value.closest ? t('in its {container}', { container: value.closest }) : t('in the link')
		return `${where}${value.selector ? ` › ${value.selector}` : ''}: ${value.attribute ? t('attribute {name}', { name: value.attribute }) : t('its text')}`
	}

	// A level's values from the page (or a JSON item's fields): name, type, where (the field),
	// the pattern on the text; each changed or removed. The examples above show what they give.
	function renderValues(spec, send, target, level, json) {
		const values = spec.values || []
		const rows = values.map((value, index) => {
			const change = (changes) => send({ type: 'value', target, level, index, changes })
			const input = (key, label) => {
				const box = el('input', { class: 'harvester-pattern', value: value[key] || '', spellcheck: 'false', 'aria-label': label, title: label, placeholder: label })
				box.addEventListener('change', () => change({ [key]: box.value }))
				return box
			}
			const kind = el('select', { 'aria-label': t('Type') }, ...['text', 'number', 'date'].map((type) => {
				const option = el('option', { value: type, text: { text: t('text'), number: t('number'), date: t('date (year, month, day)') }[type] })
				option.selected = (value.type || 'text') === type
				return option
			}))
			kind.addEventListener('change', () => change({ type: kind.value }))
			return el('li', {},
				input('name', t('Name')), ' ', kind, ' ',
				json ? input('field', t('Field of the item, e.g. issued')) : el('small', { class: 'harvester-muted', text: valueWhere(value) }),
				field(t('Pattern on its text (empty: all of it)'), input('regex', t('Pattern'))),
				el('button', { type: 'button', class: 'error', text: t('Remove'), onclick: () => send({ type: 'value-remove', target, level, index }) }))
		})
		if (!rows.length) return el('p', { class: 'harvester-muted', text: json ? t('Values: none from the items’ fields.') : t('Values from the page: none (Pick a value).') })
		return el('div', { class: 'harvester-page-values' }, el('h5', { text: json ? t('Values from the items’ fields') : t('Values from the page') }), el('ul', {}, ...rows))
	}

	// File links from a JSON listing (chosen in the Network panel): the request's address, the
	// path to its items, an item's fields for the file's address and text, the page parameter;
	// what the answer gives with them.
	function renderJson(title, spec, proposal, send) {
		const input = (key, label, help) => {
			const box = el('input', { class: 'harvester-pattern', value: spec[key] || '', spellcheck: 'false' })
			box.addEventListener('change', () => send({ type: 'json-edit', changes: { [key]: box.value } }))
			return field(label, box, help)
		}
		const page = spec.page || {}
		const param = el('input', { class: 'harvester-pattern', value: page.param || '', spellcheck: 'false', 'aria-label': t('Page parameter'), placeholder: 'page' })
		const first = el('input', { type: 'number', min: '0', value: String(page.first ?? 1), 'aria-label': t('First value') })
		const step = el('input', { type: 'number', min: '1', value: String(page.step ?? 1), 'aria-label': t('Step') })
		const post = spec.method === 'POST'
		const inBody = (part) => el('label', { title: t('The value goes into the request’s body (a field’s path such as variables.page), not into its address') }, (() => {
			const box = el('input', { type: 'checkbox', class: 'harvester-json-in' })
			box.checked = Boolean(part && part.in === 'body')
			return box
		})(), ` ${t('in the body')}`)
		const pageIn = inBody(page)
		const pages = () => send({ type: 'json-edit', changes: { page: param.value.trim() ? { param: param.value.trim(), first: Number(first.value || 0), step: Number(step.value || 1), in: pageIn.firstChild.checked ? 'body' : 'query' } : null } })
		for (const box of [param, first, step, pageIn.firstChild]) box.addEventListener('change', pages)
		const chain = spec.cursor || {}
		const cursorBox = (key, label, placeholder) => el('input', { class: 'harvester-pattern', value: chain[key] || '', spellcheck: 'false', 'aria-label': label, title: label, placeholder })
		const cursorFrom = cursorBox('from', t('Cursor: its field in the answer'), 'data.items.pageInfo.endCursor')
		const cursorTo = cursorBox('to', t('Sent as (empty: the field is the next page’s address)'), 'variables.after')
		const cursorMore = cursorBox('more', t('More pages while (a field that is true; optional)'), 'pageInfo.hasNextPage')
		const cursorIn = inBody(chain)
		const cursors = () => send({ type: 'json-edit', changes: { cursor: cursorFrom.value.trim() ? { from: cursorFrom.value, to: cursorTo.value, more: cursorMore.value, in: cursorIn.firstChild.checked ? 'body' : 'query' } : null } })
		for (const box of [cursorFrom, cursorTo, cursorMore, cursorIn.firstChild]) box.addEventListener('change', cursors)
		const request = []
		if (post) {
			const text = typeof spec.body === 'string' ? spec.body : JSON.stringify(spec.body ?? {}, null, 2)
			const body = el('textarea', { class: 'harvester-json-body', rows: '6', spellcheck: 'false', 'aria-label': t('Body') })
			body.value = text
			body.addEventListener('change', () => send({ type: 'json-edit', changes: { body: body.value } }))
			request.push(el('p', { class: 'harvester-muted', text: t('Asked for with POST ({type}), with this body; a text that is only a value’s placeholder, such as {year}, takes the value.', { type: spec.content_type || 'application/json', year: '"{year}"' }) }), body)
		}
		const headers = Object.entries(spec.headers || {})
		if (headers.length) {
			request.push(el('p', { class: 'harvester-muted', text: t('Headers it sends:') }), el('ul', {}, ...headers.map(([name, value]) => el('li', {},
				el('code', { text: name }), ' ',
				el('span', { text: typeof value === 'string' ? value : t('read when the job runs from the {source} {name}', { source: { cookie: t('cookie'), meta: t('page’s meta'), storage: t('page’s storage') }[value.from] || value.from, name: value.name }) })))))
		}
		if (proposal && proposal.dropped && proposal.dropped.length) {
			request.push(el('p', { class: 'harvester-error', text: t('The page also sent {names}, a token found in none of its cookies, meta or storage: the job leaves it out and sends its cookies. If the listing then answers 401 or 403, it needs a sign-in in the browser (steps) or cookies in the job’s account.', { names: proposal.dropped.join(', ') }) }))
		}
		const fieldName = el('input', { class: 'harvester-pattern', spellcheck: 'false', 'aria-label': t('Field'), placeholder: t('e.g. issued or meta.date') })
		const box = el('div', { class: 'harvester-level', 'data-target': 'links', 'data-level': '0' },
			el('div', { class: 'harvester-level-head' }, el('h4', { text: `${title} (JSON)` }),
				el('button', { type: 'button', class: 'error', text: t('Remove'), title: t('Remove this pick'), onclick: () => send({ type: 'remove', target: 'links', level: 0 }) })),
			el('p', { class: 'harvester-muted', text: t('The files are the items of an answer the page fetched: the job asks for it itself (with its cookies), page by page.') }),
			input('url', t('Request'), t('Its page parameter is set for each page; {year} and the like take the levels’ values.', { year: '{year}' })),
			input('items', t('Items (path in the answer, e.g. data.items)')),
			input('file', t('File’s address (field of an item, e.g. pdf.href)')),
			input('text', t('Text (field of an item, e.g. title)')),
			input('pattern', t('Pattern on the file’s address (optional; named groups become values)')),
			...request,
			el('div', { class: 'harvester-toolbar' }, el('span', { text: `${t('Pages: parameter')} ` }), param, el('span', { text: ` ${t('from')} ` }), first, el('span', { text: ` ${t('step')} ` }), step, ...(post ? [' ', pageIn] : [])),
			el('div', { class: 'harvester-toolbar' }, el('span', { text: `${t('Or a cursor:')} ` }), cursorFrom, ' → ', cursorTo, ' ', cursorMore, ...(post ? [' ', cursorIn] : [])))
		if (proposal && proposal.error) box.append(el('p', { class: 'harvester-error', text: proposal.error }))
		if (proposal && proposal.chosen) {
			const counts = { items: proposal.items, found: proposal.found, matching: proposal.matching }
			const single = spec.cursor ? t('The answer gives no next cursor: one page.') : t('One page (no page parameter).')
			box.append(el('p', { text: `${t('In the answer: {items} items, {found} with a file’s address; {matching} match the pattern.', counts)} ${proposal.following ? t('The next page:') : single}` }))
			if (proposal.following) box.append(el('code', { text: proposal.following }))
			if (proposal.sample) box.append(el('details', {}, el('summary', { text: t('The first item') }), el('code', { text: proposal.sample })))
			box.append(examplesList(proposal))
		} else {
			box.append(el('p', { class: 'harvester-muted', text: t('Choose its answer in the Network panel again to see what it gives.') }))
		}
		box.append(renderValues(spec, send, 'links', 0, true),
			el('div', { class: 'harvester-toolbar' }, fieldName,
				el('button', { type: 'button', text: t('Add a value'), onclick: () => { if (fieldName.value.trim()) send({ type: 'value-add', target: 'links', level: 0, field: fieldName.value.trim() }) } })))
		return box
	}

	// The next page: one link, or numbered pages (‹ 1 [2] 3 ›: the number one higher than the
	// current page's), with what this page shows and the switch between the two when both fit.
	function renderNext(box, spec, proposal, send) {
		const where = Number(spec.level || 0) === 0 ? t('On the listing.') : t('On the pages of level {level}.', { level: spec.level })
		if (spec.kind === 'number') {
			box.append(el('p', { text: `${t('Numbered pages: the number after the current page’s is followed.')} ${where}` }))
			if (proposal && proposal.kind === 'number') {
				box.append(el('p', {
					text: proposal.following != null
						? t('On this page: numbers {numbers}; this is page {current}, the next is {next}.', { numbers: proposal.numbers.join(' '), current: proposal.current, next: proposal.following })
						: t('On this page: numbers {numbers}; this is page {current}, the next is none (the last page).', { numbers: proposal.numbers.join(' '), current: proposal.current }),
				}))
			}
			if (spec.buttons) box.append(el('p', { class: 'harvester-muted', text: t('The numbers are buttons: the job clicks them in the browser.') }))
			if (proposal && proposal.link) box.append(el('button', { type: 'button', text: t('Use the picked link alone'), onclick: () => send({ type: 'next-kind', kind: 'link' }) }))
		} else {
			box.append(el('p', { text: `${t('The link to the next page.')} ${where}` }))
			if (proposal && proposal.number) box.append(el('button', { type: 'button', text: t('Use numbered pages'), onclick: () => send({ type: 'next-kind', kind: 'number' }) }))
		}
		return box
	}

	// What the recipe's test found: the listing pages it read with the links matched, the first
	// file links with the file names the job would give them, the problems, and the page the
	// browser showed when a step failed.
	// What a page is saved as, in words.
	const SAVED_AS = { pdf: 'PDF', html: 'HTML', markdown: 'Markdown' }

	// The test's outcome in one line, first, coloured by it: failed (a problem, a failed step or
	// file), no file links, stopped, or the file links found.
	function testSummary(result) {
		const problems = result.problems.length + (result.probe && !result.probe.ok ? 1 : 0)
		const [outcome, text] = result.ended === 'stopped' ? ['stopped', t('Stopped.')]
			: problems || result.ended === 'failed' ? ['failed', n('Failed: %n problem, listed below.', 'Failed: %n problems, listed below.', Math.max(problems, 1))]
				: !result.files ? ['empty', t('No file links found.')]
					: ['ok', n('OK: %n file link found.', 'OK: %n file links found.', result.files)]
		return el('p', { class: `harvester-test-summary harvester-test-${outcome}`, text })
	}

	function renderTest(result) {
		const ended = {
			complete: t('It went through the whole site.'),
			files: t('It stopped at the first file links (a test lists a few).'),
			pages: t('It stopped after its pages (a test reads a few).'),
			time: t('It stopped: its time was up.'),
			stopped: t('Stopped.'),
			failed: t('It failed.'),
		}[result.ended] || ''
		const found = n('%n page', '%n pages', result.pages.length)
		const counts = (page) => ({ level: page.level === 0 ? t('Listing') : t('Level {level}', { level: page.level }), page: page.page, matching: page.matching, found: page.found })
		const box = el('div', { class: 'harvester-test' },
			el('h3', { text: t('Test') }),
			testSummary(result),
			el('p', { text: `${n('%n file link on {pages}.', '%n file links on {pages}.', result.files, { pages: found })} ${ended}`.trim() }))
		const choices = result.choices || []
		if (choices.length) box.append(el('p', { text: n('It goes through %n choice: {choices}', 'It goes through %n choices: {choices}', choices.length, { choices: choices.join(', ') }) }))
		if (result.as_job) box.append(el('p', { class: 'harvester-muted', text: t('As the job runs: a new browser with the job’s stored account, in the recipe’s window.') }))
		if (result.probe) {
			const probe = result.probe
			box.append(probe.ok
				? el('p', { text: t('The first file’s first {size} KB: {type}, of the job’s file types.', { size: Math.round(probe.bytes / 1000), type: probe.type || t('no type given') }) })
				: el('p', { class: 'harvester-error' }, el('span', { text: `${t('The first file: {problem}', { problem: probe.problem })} ` }), el('code', { text: probe.address || probe.url })))
		}
		if (result.http) box.append(el('p', { class: 'harvester-muted', text: t('The job reads these pages without the browser (no steps): a page built by JavaScript may show it fewer links than here. Turn on “Use the browser” then.') }))
		if (result.problems.length) box.append(el('ul', { class: 'harvester-error' }, ...result.problems.map((problem) => el('li', { text: problem }))))
		// Optional steps it went on without: no problems of the recipe, but worth knowing.
		const skipped = result.skipped || []
		if (skipped.length) {
			box.append(el('div', { class: 'harvester-skipped' },
				el('p', { class: 'harvester-muted', text: t('Optional steps skipped:') }),
				el('ul', {}, ...skipped.map((step) => el('li', { class: 'harvester-muted', text: t('step {number} ({label}): {why}', { number: step.number, label: step.label, why: step.why }) })))))
		}
		// Elements an alternative selector found: the recipe still works, but the site changed.
		const repaired = result.repaired || []
		if (repaired.length) {
			box.append(el('div', { class: 'harvester-repaired' },
				el('p', { class: 'harvester-muted', text: t('Found by an alternative selector (the recipe is unchanged):') }),
				el('ul', {}, ...repaired.map((line) => el('li', { class: 'harvester-muted', text: line })))))
		}
		box.append(el('ol', { class: 'harvester-test-pages' }, ...result.pages.map((page) => el('li', {},
			el('span', { text: `${page.kind === 'follow' ? t('{level}, page {page}: {matching} of {found} links to follow', counts(page)) : t('{level}, page {page}: {matching} of {found} file links', counts(page))} ` }),
			el('code', { text: page.url }),
			page.choice ? el('small', { class: 'harvester-muted', text: ` ${t('(choice “{choice}”)', { choice: page.choice })}` }) : '',
			page.files.length ? el('ul', {}, ...page.files.map((file) => el('li', {},
				file.problem ? el('span', { class: 'harvester-error', text: `${file.url}: ${file.problem}` }) : el('span', { text: file.path, title: file.url }),
				file.click ? el('small', { class: 'harvester-muted', text: ` ${t('(clicked: “{text}”)', { text: file.text })}` }) : '',
				file.save ? el('small', { class: 'harvester-muted', text: ` ${t('(the page saved as {kind})', { kind: SAVED_AS[file.save] || file.save })}` }) : '',
				file.capture && file.capture.length ? el('small', { class: 'harvester-muted', text: ` ${t('(its files of the types {types})', { types: file.capture.join(', ') })}` }) : ''))) : ''))))
		if (result.pages.some((page) => page.files.some((file) => file.click))) {
			box.append(el('p', { class: 'harvester-muted', text: t('File links that are clicked are listed, not clicked: the test downloads nothing (a run clicks each).') }))
		}
		if (result.pages.some((page) => page.files.some((file) => file.save || (file.capture && file.capture.length)))) {
			box.append(el('p', { class: 'harvester-muted', text: t('Pages to save, or to keep the files of, are listed, not opened: the test saves nothing (a run opens each; a title in the file name comes from the page then).') }))
		}
		if (result.shot) {
			box.append(el('p', { text: `${result.shot.what}: ${result.shot.title || ''} ` }, el('code', { text: result.shot.url })),
				el('img', { class: 'harvester-shot-image', src: `data:image/jpeg;base64,${result.shot.image}`, alt: t('What the browser showed then') }))
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
	function wireScreen(screen, send, socket) {
		watchScreen(screen, send, socket)
		// Every key goes to the site, Tab and Escape too: Escape twice within a second leaves the
		// screen for the page's next control, as the hint says while the screen has the keyboard.
		wireScreens += 1
		const hint = el('small', { class: 'harvester-keys-hint', id: `harvester-keys-hint-${wireScreens}`, text: t('Keys go to the site; press Esc twice to leave') })
		const keys = el('textarea', {
			class: 'harvester-keys', 'aria-label': t('Keyboard of the remote browser'), 'aria-describedby': hint.id,
			autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
		})
		// Until its first frame (or instead of it, when the browser cannot start or is taken),
		// the screen says what is going on, not an empty image.
		const notice = el('div', { class: 'harvester-screen-notice', text: t('Starting the browser…') })
		const box = el('div', { class: 'harvester-screen-box' }, screen, notice, keys, hint)
		// A frame: a binary message's JPEG (its object URL, the one before revoked), or base64.
		let frameUrl = null
		screen.frame = (data) => {
			const url = typeof data === 'string' ? `data:image/jpeg;base64,${data}` : URL.createObjectURL(data)
			screen.src = url
			if (frameUrl) URL.revokeObjectURL(frameUrl)
			frameUrl = typeof data === 'string' ? null : url
			notice.hidden = true
		}
		socket.binaryType = 'blob'
		socket.addEventListener('message', (event) => {
			if (typeof event.data !== 'string' && !left(screen, socket)) screen.frame(event.data)
		})
		socket.addEventListener('close', () => {
			if (frameUrl) URL.revokeObjectURL(frameUrl)
			frameUrl = null
		})
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

		let escaped = 0
		keys.addEventListener('keydown', (event) => {
			if (event.key === 'Escape' && !event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey) {
				if (Date.now() - escaped < 1000) {
					event.preventDefault()
					escaped = 0
					leaveScreen(box)
					return
				}
				escaped = Date.now()
			}
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

	// The keyboard out of a browser's screen (Escape twice): to the next control after it on the
	// page, or the first one before it when none follows.
	let wireScreens = 0
	function leaveScreen(box) {
		const controls = [...document.querySelectorAll('button, input, select, textarea, a[href], summary')]
			.filter((node) => !box.contains(node) && !node.disabled && node.getClientRects().length)
		const next = controls.find((node) => box.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)
		const target = next || controls.reverse().find((node) => box.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING)
		if (target) target.focus()
		else document.activeElement.blur()
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
		const button = el('button', { class: 'harvester-wide-switch', title: t('Full window (again: back)'), 'aria-label': t('Full window') }, svg)
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
			title: t('Hide or show the controls'),
			'aria-label': t('Hide or show the controls'),
			onclick: () => bar.classList.toggle('harvester-overlay-hidden'),
		})
		bar.prepend(collapse)
		return bar
	}

	// The screen's frames only while someone sees them: the browser stops sending them while the
	// page is hidden (another tab, a minimized window) and starts again when it shows. Once the
	// screen left the page (another view of the app, opened without the view's Cancel), its
	// connection closes at once, which ends the browser on the server and frees it, instead of
	// at the browser's idle time limit.
	function watchScreen(screen, send, socket) {
		let shown = false
		const visibility = () => send({ type: 'screen', visible: document.visibilityState === 'visible' })
		const watch = new MutationObserver(() => {
			if (screen.isConnected) shown = true
			else if (shown) socket.close()
		})
		const stop = () => {
			watch.disconnect()
			document.removeEventListener('visibilitychange', visibility)
		}
		watch.observe(document.body, { childList: true, subtree: true })
		document.addEventListener('visibilitychange', visibility)
		socket.addEventListener('open', () => { if (document.visibilityState !== 'visible') visibility() })
		socket.addEventListener('close', stop)
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
		const scope = el('select', { 'aria-label': t('Which cookies') },
			el('option', { value: 'site', text: t('This site’s') }), el('option', { value: 'all', text: t('All') }))
		const load = () => send({ type: 'cookies', all: everything })
		scope.addEventListener('change', () => { everything = scope.value === 'all'; load() })
		const readable = (expires) => expires ? new Date(expires * 1000).toLocaleString() : t('session')

		// Add (cookie null) or change a cookie. A value left empty while changing keeps the one the
		// panel never showed.
		function edit(cookie) {
			const input = (value, attributes = {}) => { const node = el('input', attributes); node.value = value; return node }
			const name = input(cookie ? cookie.name : '', { 'aria-label': t('Name') })
			const value = input(cookie && key(cookie) in values ? values[key(cookie)] : '', {
				'aria-label': t('Value'), placeholder: cookie && !(key(cookie) in values) ? t('(unchanged)') : '', autocomplete: 'off', spellcheck: 'false',
			})
			const domain = input(cookie ? cookie.domain : host, { 'aria-label': t('Domain') })
			const path = input(cookie ? cookie.path : '/', { 'aria-label': t('Path') })
			const expiresAt = cookie && cookie.expires ? new Date(cookie.expires * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ''
			const expires = input(expiresAt, { type: 'datetime-local', 'aria-label': t('Expires (empty: the session)') })
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
			form.replaceChildren(el('h4', { text: cookie ? t('Change {name}', { name: cookie.name }) : t('Add a cookie') }),
				field(t('Name'), name), field(t('Value'), value), field(t('Domain'), domain, t('This site, a parent (example.com) or a subdomain; a leading dot: its subdomains too.')),
				field(t('Path'), path), field(t('Expires'), expires, t('Empty: until the browser closes (a session cookie).')),
				el('label', {}, secure, ' Secure '), el('label', {}, httpOnly, ' HttpOnly '), field('SameSite', sameSite),
				el('div', { class: 'harvester-toolbar' },
					el('button', { type: 'button', class: 'primary', text: cookie ? t('Save cookie') : t('Add cookie'), onclick: save }),
					el('button', { type: 'button', text: t('Cancel'), onclick: () => form.replaceChildren() })))
		}

		function render() {
			list.replaceChildren(...(groups.length ? groups : [null]).map((group) => {
				if (!group) return el('p', { class: 'harvester-muted', text: everything ? t('No cookies.') : host ? t('No cookies for {host}.', { host }) : t('No cookies for this page.') })
				return el('div', { class: 'harvester-cookie-group', 'data-domain': group.domain },
					el('div', { class: 'harvester-level-head' }, el('h4', { text: group.domain }),
						el('button', { type: 'button', class: 'error', text: t('Delete all'), title: t('Delete every cookie of {domain}', { domain: group.domain }), onclick: () => send({ type: 'cookies-delete-site', domain: group.domain, all: everything }) })),
					el('table', { class: 'harvester-cookies' },
						el('tr', {}, ...[t('Name'), t('Value'), t('Path'), t('Expires'), t('Flags'), ''].map((text) => el('th', { text }))),
						...group.cookies.map((cookie) => {
							const known = key(cookie) in values
							const flags = [cookie.secure ? 'Secure' : '', cookie.httpOnly ? 'HttpOnly' : '', `SameSite=${cookie.sameSite}`].filter(Boolean).join(' ')
							return el('tr', { 'data-cookie': cookie.name },
								el('td', {}, el('code', { text: cookie.name })),
								el('td', {}, known ? el('code', { class: 'harvester-cookie-value', text: values[key(cookie)] }) : el('span', { class: 'harvester-muted', text: `•••• (${cookie.size})` }),
									el('button', { type: 'button', class: 'harvester-link', text: known ? t('Hide') : t('Show'), onclick: () => {
										if (known) { delete values[key(cookie)]; render() } else send({ type: 'cookie-show', name: cookie.name, domain: cookie.domain, path: cookie.path })
									} })),
								el('td', { text: cookie.path }),
								el('td', { text: readable(cookie.expires) }),
								el('td', { text: flags }),
								el('td', {},
									el('button', { type: 'button', text: t('Edit'), onclick: () => edit(cookie) }),
									el('button', { type: 'button', class: 'error', text: t('Delete'), onclick: () => send({ type: 'cookie-delete', name: cookie.name, domain: cookie.domain, path: cookie.path, all: everything }) })))
						})))
			}))
		}

		const actions = el('div', { class: 'harvester-toolbar' }, scope,
			el('button', { type: 'button', text: t('Refresh'), onclick: load }),
			el('button', { type: 'button', text: t('Add'), onclick: () => edit(null) }))
		if (intoAccount) {
			actions.append(el('button', {
				type: 'button',
				text: t('Into the account'),
				title: t('These cookies (this site’s, or all) as a cookies.txt into the job’s account: stored once you save it'),
				onclick: () => send({ type: 'cookies-export', all: everything }),
			}))
		}
		if (keep) {
			actions.append(el('button', {
				type: 'button',
				text: t('Use for this job'),
				title: t('Keep the browser’s session (cookies, local storage) for the job’s next run; the browser stays open'),
				onclick: () => send({ type: 'cookies-keep' }),
			}))
		}
		const box = el('div', { class: 'harvester-cookies-panel', hidden: '' },
			el('h3', { text: t('Cookies') }),
			el('p', { class: 'harvester-muted', text: t('The browser’s cookies, changed at once. Values stay hidden until you show one: they are often the sign-in itself.') }),
			actions, error, status, list, form)
		const button = el('button', {
			type: 'button',
			text: t('Cookies'),
			title: t('See, change or take over the browser’s cookies'),
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
			} else if (data.type === 'cookies-kept') status.textContent = t('Kept for the job: its next run starts with this session.')
			else return false
			return true
		}
		return { button, box, handle }
	}

	function networkLine() {
		const line = el('p', { class: 'harvester-muted harvester-network', text: t('Checking which address sites see…') })
		line.show = (data) => {
			const where = data.proxied ? t('through {proxy}', { proxy: proxyName(data.via) }) : t('directly, from the server')
			const address = data.country ? `${data.ip} (${data.country})` : data.ip
			line.textContent = data.error
				? t('Sites see: unknown ({where}; the check failed: {error})', { where, error: data.error })
				: t('Sites see {address}, {where}.', { address, where })
			line.classList.toggle('harvester-proxied', Boolean(data.proxied && !data.error))
		}
		return line
	}

	// What a job's browser showed when a step failed or a page asked for a person: the picture,
	// the page's address and title; from here the browser opens live at that page.
	async function showShot(root, job) {
		const view = el('div', { class: 'harvester-shot' }, el('p', { class: 'harvester-muted', text: t('Loading…') }))
		root.replaceChildren(view)
		let shot
		try {
			shot = await api('GET', `/jobs/${job.id}/shot`)
		} catch (e) {
			view.replaceChildren(el('p', { class: 'harvester-error', text: e.message }),
				el('button', { text: t('Back'), onclick: () => show(root) }))
			return
		}
		if (!root.contains(view)) return
		view.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: t('{name}: what the browser saw', { name: job.name }) }),
					el('p', { class: 'harvester-muted', text: `${shot.what} · ${when(shot.at)}` })),
				el('div', { class: 'harvester-actions' },
					el('button', { class: 'primary', text: t('Open in the browser'), title: t('The job’s browser at this page, with its session, to look live'), onclick: () => openSession(root, job, 'shot') }),
					el('button', { text: t('Back'), onclick: () => show(root) }))),
			el('p', {}, el('strong', { text: shot.title || t('(no title)') }), el('br'), el('code', { text: shot.url })),
			el('img', { class: 'harvester-shot-image', src: `data:image/jpeg;base64,${shot.image}`, alt: t('The page {address} as the job’s browser showed it', { address: shot.url }) }))
	}

	// The remote browser's navigation: Back, Forward and Reload, buttons as any other (Tab and
	// Enter or Space reach them); the server records none of them outside the mode record.
	function navigation(send) {
		return [['back', t('Back'), t('The page before')], ['forward', t('Forward'), t('The page after')], ['reload', t('Reload'), t('Load this page again')]]
			.map(([type, text, title]) => el('button', { type: 'button', text, title, onclick: () => send({ type }) }))
	}

	// A job's browser with the job's session: at the page that asked for a person (the person
	// answers it), or with at 'shot' at the page of its last failed step (to look live). "Done"
	// keeps the session and runs the job again.
	function openSession(root, job, at = '') {
		const socket = new WebSocket(`${RECORDER}?job=${job.id}${at ? `&at=${encodeURIComponent(at)}` : ''}`)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', alt: t('The job’s browser') })
		const url = el('input', { class: 'harvester-url', value: job.challenge_url || '', 'aria-label': t('Address') })
		url.addEventListener('keydown', (event) => { if (event.key === 'Enter') send({ type: 'goto', url: url.value }) })
		const starting = t('Starting the browser…')
		const message = el('p', { class: 'harvester-muted', text: starting })
		const network = networkLine()
		const done = el('button', {
			class: 'primary',
			text: t('Done'),
			title: t('Keep this session for the job and run it again'),
			onclick: () => { done.disabled = true; send({ type: 'save-session' }) },
		})
		// The job's account takes cookies when its source has the field; stored only once the
		// person confirms (there is no form here to save it with).
		const source = sources.find((s) => s.name === job.source)
		const intoAccount = source && source.credentials.includes('cookies')
			? async (text, count) => {
				if (!confirm(n('Store this cookie as the account’s Cookies of “{name}”? It replaces the ones stored, and the job’s kept session is forgotten.', 'Store these %n cookies as the account’s Cookies of “{name}”? They replace the ones stored, and the job’s kept session is forgotten.', count, { name: job.name }))) return t('Not stored.')
				await api('PUT', `/jobs/${job.id}/credentials`, { cookies: text })
				return n('%n cookie stored in the job’s account.', '%n cookies stored in the job’s account.', count)
			}
			: null
		const cookies = cookiesPanel(send, intoAccount, true)
		socket.addEventListener('message', (event) => {
			// A frame (a binary message): the screen shows it (wireScreen).
			if (typeof event.data !== 'string') {
				if (message.textContent === starting) message.textContent = ''
				return
			}
			const data = JSON.parse(event.data)
			if (cookies.handle(data)) return
			if (data.type === 'frame') {
				if (left(screen, socket)) return
				screen.frame(data.data)
				if (message.textContent === starting) message.textContent = ''
			} else if (data.type === 'cursor') screen.style.cursor = data.cursor
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') { message.textContent = shown(data.message, data.message_l10n); done.disabled = false; screen.say(message.textContent) }
			else if (data.type === 'session-saved') { socket.close(); show(root) }
			else if (data.type === 'viewport') screen.resized(data)
			else if (data.type === 'network') network.show(data)
		})
		socket.addEventListener('close', () => { if (root.contains(screen)) { message.textContent ||= t('The browser closed.'); screen.say(message.textContent) } })
		const box = wireScreen(screen, send, socket)
		const container = el('div', { class: 'harvester-recorder' })
		container.append(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: at === 'shot' ? t('{name}: in the job’s browser', { name: job.name }) : t('{name}: continue in the browser', { name: job.name }) }),
					el('p', {
						class: 'harvester-muted',
						text: at === 'shot'
							? t('The job’s browser with its session, at the page where it stopped. Look around (nothing is recorded); Done keeps this session and runs the job again, Cancel leaves it as it was.')
							: t('The site asks for a person. Answer it here as you would in your own browser (and sign in, if it asks); then press Done. The job keeps this session and runs again.'),
					}))),
			overlay(
				el('div', { class: 'harvester-toolbar' }, ...navigation(send), url,
					el('button', { type: 'button', text: t('Go'), onclick: () => send({ type: 'goto', url: url.value }) }),
					fullWindow(container, box, send), cookies.button, done,
					el('button', { text: t('Cancel'), onclick: () => { socket.close(); show(root) } })),
				network, message, cookies.box),
			el('div', { class: 'harvester-stage' }, box))
		root.replaceChildren(container)
		screen.focusKeys()
	}

	// The recorder: the browser's screen, the person's mouse and keys to it, and the recipe so far;
	// settings() gives the form's settings the recipe's test uses (the file names, the pages, the
	// file types), jobId() the saved job's id (the test as the job runs uses its account).
	function openRecorder(root, page, textarea, apply, settings = () => ({}), jobId = () => null) {
		let recipe = {}
		try {
			recipe = JSON.parse(textarea.value || '{}')
		} catch (e) {}
		let proposals = {}
		// The recorder's warnings of addresses with a token of this visit (spent when the job runs).
		let warnings = []
		let testing = false
		let tested = null
		// What the test did with each step (by index): its last event from the app ({state, attempt,
		// of, seconds, why}, site.step_event), a pause's end (ends); the step whose editor is open.
		let played = {}
		let editing = null
		let focusEditor = false
		// The steps as the test played them: an edit that moves, adds or removes steps forgets the
		// marks (they would fall on other steps).
		let playedShape = ''
		const shape = () => JSON.stringify((recipe.steps || []).map((step) => [step.action, step.selector || step.url || '']))
		// The page's answers in JSON (the Network panel), and those shown (Preview): id → the
		// server's {text, truncated, kept}, or null while it is on its way.
		let requests = []
		const previews = new Map()
		const suggestions = {}
		// A recipe with steps opens in Browse: recording asks first where its new steps go (after
		// which step, and whether the later ones stay), never replacing them unasked.
		let placed = !(recipe.steps || []).length
		// A password the recipe holds (the recorder's warning on load).
		const leaks = el('div', { class: 'harvester-warning', 'aria-live': 'polite' })
		// The form's "HTTP/1.1 only", "Through the proxy", "Disguise the browser" and "Dismiss cookie
		// banners", known before the browser starts (a saved job's own options win for its recordings).
		const flags = ['http1', 'home', 'stealth', 'consent'].map((name) => page.querySelector(`[data-option="${name}"]`))
		const query = flags.filter((box) => box && box.checked).map((box) => `${box.dataset.option}=1`).join('&')
		const socket = new WebSocket(query ? `${RECORDER}?${query}` : RECORDER)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', alt: t('The recorder’s browser') })
		const url = el('input', { class: 'harvester-url', placeholder: t('https://… the site’s first page'), 'aria-label': t('Address') })
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
				return n('%n cookie in the account’s Cookies field: stored with the job when you save it.', '%n cookies in the account’s Cookies field: stored with the job when you save it.', count)
			}
			: null, false)
		const help = {
			record: t('Use the site as usual: sign in and go to the listing. Clicks, typing, addresses typed and Back or Forward are recorded; scrolling is not (add “scroll to load more” for a listing that grows as it is scrolled).'),
			browse: t('Go anywhere without recording (into a year, an issue, …) to pick there.'),
			follow: t('Click one link that leads to a further listing (a year, a category): similar links are picked with it.'),
			links: t('Click one file link: the similar ones are picked with it, and a pattern is proposed.'),
			next: t('Click the link to the next page of this listing (if there is one); for numbered pages (1 2 3 …), click the number of the next page.'),
		}
		const modeHelp = el('small', { text: (recipe.steps || []).length ? help.browse : help.record })
		// Where the person acts: the page, or a frame in it (recorder.js reports it as the mouse
		// or the focus comes in).
		const where = el('small', { class: 'harvester-where', 'aria-live': 'polite', text: t('Acting in: the page') })
		const showWhere = (frame) => {
			where.textContent = frame ? t('Acting in a frame: {host} (level {level})', { host: frame.host || t('(no address)'), level: frame.level }) : t('Acting in: the page')
		}
		const scroll = el('button', {
			type: 'button',
			text: t('Add: scroll to load more'),
			title: t('A step: scroll to the end of the page until no more comes (a listing that loads more as it is scrolled)'),
			onclick: () => send({ type: 'scroll' }),
		})
		// The steps of yours, where the recording is: an approval in your app (once you approved
		// and the site went on: it waits for this address), and a handover (before what only you
		// can do in the page).
		const approve = el('button', {
			type: 'button',
			text: t('Add: wait for my approval'),
			title: t('After you approved the sign-in in your app and the site went on: a step that waits for your approval when the job runs, until the page shows this address'),
			onclick: () => send({ type: 'person', action: 'approve', label: t('Approve the sign-in in your app') }),
		})
		const handover = el('button', {
			type: 'button',
			text: t('Add: hand over to me'),
			title: t('Before what only you can do (a passkey, a check): a step that stops the job here, for you to go on in its browser'),
			onclick: () => send({ type: 'person', action: 'handover', label: t('Sign in yourself (a passkey)') }),
		})
		const radios = {}
		let beforeUntil = null
		const labels = [['record', t('Record steps')], ['browse', t('Browse')], ['follow', t('Pick links to follow')], ['links', t('Pick file links')], ['next', t('Pick next-page link')]]
		const placing = el('div', { class: 'harvester-placing' })
		let current = placed ? 'record' : 'browse'
		// Steps are added only while recording.
		for (const button of [scroll, approve, handover]) button.disabled = current !== 'record'
		const choose = (mode, text, level, target) => {
			// Picking a value has no radio of its own: it is a level's, started from its box. Recording
			// on a recipe with steps waits for where the new ones go: the mode stays as it was.
			const shown = mode === 'record' && !placed ? current : mode
			for (const [name, radio] of Object.entries(radios)) radio.checked = name === shown
			if (shown !== mode) {
				askPlace()
				return
			}
			placing.replaceChildren()
			current = mode
			beforeUntil = null
			send(level === undefined ? { type: 'mode', mode } : { type: 'mode', mode, level, target })
			modeHelp.textContent = text || help[mode]
			for (const button of [scroll, approve, handover]) button.disabled = mode !== 'record'
			screen.focusKeys()
		}
		// The element an approval or a handover waits for, picked on the page: then back to the
		// mode before (beforeUntil).
		const pickUntil = (index) => {
			beforeUntil = beforeUntil || current
			for (const radio of Object.values(radios)) radio.checked = false
			current = 'until'
			send({ type: 'mode', mode: 'until', step: index })
			modeHelp.textContent = t('Click the element the page shows once you are through (your accounts, your name): the job waits for it.')
			for (const button of [scroll, approve, handover]) button.disabled = true
			screen.focusKeys()
		}
		// Where the new steps of a recipe with steps go: after a step (the last, unless chosen),
		// keeping the later ones; from there on, instead of the later ones; or instead of all.
		function askPlace() {
			const count = (recipe.steps || []).length
			const after = el('select', { 'aria-label': t('After which step') },
				...Array.from({ length: count + 1 }, (_, number) => el('option', { value: String(number), text: number ? t('after step {number}', { number }) : t('before step 1') })))
			after.value = String(count)
			const start = (replace, at) => {
				send({ type: 'record-at', after: at === undefined ? Number(after.value) : at, replace })
				placed = true
				choose('record')
			}
			placing.replaceChildren(
				el('span', { text: `${t('This recipe has steps. Record new ones')} ` }), after, ' ',
				el('button', { type: 'button', class: 'primary', text: t('Append'), title: t('The new steps go in there; the steps after it stay after them'), onclick: () => start(false) }),
				el('button', { type: 'button', text: t('Replace from there'), title: t('The steps after that place go; the new ones take their place'), onclick: () => start(true) }),
				el('button', { type: 'button', text: t('Replace all'), title: t('All steps go (the sign-in too); the new ones are the recipe’s'), onclick: () => start(true, 0) }),
				el('button', { type: 'button', text: t('Cancel'), onclick: () => placing.replaceChildren() }))
			after.focus()
		}
		for (const [mode, label] of labels) {
			const radio = el('input', { type: 'radio', name: 'harvester-mode', value: mode })
			radio.checked = mode === current
			radio.addEventListener('change', () => choose(mode))
			radios[mode] = radio
			modes.append(el('label', {}, radio, ` ${label} `))
		}
		// Pick again: the pick's mode; a level's links replace that level's, the later ones stay.
		const repick = (target, level, mode) => {
			if (mode === 'value') {
				choose('value', target === 'links'
					? t('Click a text near one of the file links (outlined): in a link, or in its row or card. It becomes a value (a date, a title); click more for more.')
					: t('Click a text near one of the links of level {level} (outlined): in a link, or in its row or card. It becomes a value (a date, a title); click more for more.', { level: level + 1 }), level, target)
			} else if (target === 'follow') {
				choose('follow', level === 0
					? t('Go to the listing (Browse) and click one of the links to follow: they replace level {next}’s.', { next: level + 1 })
					: t('Go to a page of level {level} (Browse) and click one of the links to follow: they replace level {next}’s.', { level, next: level + 1 }), level)
			} else choose(target)
		}
		const test = el('button', {
			type: 'button',
			text: t('Test the recipe'),
			title: t('Walk the recipe here with this browser’s session, without downloading: the pages, the file links and their names'),
			onclick: () => {
				if (testing) send({ type: 'test-stop' })
				else send({ type: 'test', recipe, settings: settings() })
			},
		})
		// As the job runs: a new browser at the recipe's window, the saved job's account (the app
		// adds it; this page never has it), the first file's first bytes fetched.
		const testJob = el('button', {
			type: 'button',
			text: t('Test as the job runs'),
			title: jobId() ? t('A new browser with the job’s stored account and window: the sign-in replayed, the first file’s start fetched') : t('Save the job first: this test signs in with its stored account'),
			onclick: () => send({ type: 'test', recipe, settings: settings(), as_job: true, job: jobId() }),
		})
		testJob.disabled = !jobId()
		const networkBox = el('div', { class: 'harvester-requests', hidden: '' })
		const renderRequests = () => networkBox.replaceChildren(
			el('p', { class: 'harvester-muted', text: requests.length ? t('The page’s requests answered with JSON, GET and POST (newest last). One whose items are the files can be the file links.') : t('No answers in JSON yet: browse the listing, its requests show here.') }),
			el('ul', {}, ...requests.map((request) => el('li', {},
				request.method === 'POST' ? el('strong', { text: 'POST ' }) : '',
				el('code', { text: request.url }), ` ${(request.size / 1000).toFixed(1)} KB (${request.status}) `,
				request.summary ? el('small', { class: 'harvester-muted', text: `${request.summary} ` }) : '',
				el('button', {
					type: 'button',
					text: previews.has(request.id) ? t('Hide') : t('Preview'),
					'aria-expanded': String(previews.has(request.id)),
					onclick: () => {
						if (previews.has(request.id)) previews.delete(request.id)
						else { previews.set(request.id, null); send({ type: 'json-preview', id: request.id }) }
						renderRequests()
					},
				}),
				' ',
				el('button', { type: 'button', text: t('Use as the file links'), onclick: () => send({ type: 'json-pick', id: request.id }) }),
				...requestPreview(request.id)))))
		// What Preview shows of an answer: its JSON indented, scrollable.
		const requestPreview = (id) => {
			if (!previews.has(id)) return []
			const shown = previews.get(id)
			if (shown === null) return [el('p', { class: 'harvester-muted', text: t('Loading…') })]
			if (!shown.kept) return [el('p', { class: 'harvester-muted', text: shown.dropped ? t('This answer is no longer kept: newer answers took its place (the panel keeps some 8 MB of them).') : t('This answer is not kept (larger than 2 MB, or gone): its content cannot be shown.') })]
			return [
				el('pre', { class: 'harvester-preview', text: shown.text }),
				...(shown.truncated ? [el('p', { class: 'harvester-muted', text: t('Only its beginning is shown.') })] : []),
			]
		}
		const networkButton = el('button', {
			type: 'button',
			text: t('Network'),
			title: t('The answers in JSON to the page’s requests (XHR, fetch): a listing a page builds from one'),
			'aria-expanded': 'false',
			onclick: () => {
				networkBox.hidden = !networkBox.hidden
				networkButton.setAttribute('aria-expanded', String(!networkBox.hidden))
				renderRequests()
			},
		})

		// A step of the list, with its switch "optional" (best effort: the job goes on without it)
		// and, for a click that looks like a consent banner's, the hint to make it so; buttons to
		// move it up or down and to remove it; for a fill what it fills in (a value of its own,
		// edited here, or the account's user name, password or one-time code); a list's choice to
		// edit; the address a step waits for, to drop. The recorder holds the steps: each change
		// goes to it, and its recipe comes back (the switch shows at once).
		function renderStep(step, index, count) {
			const edit = (change) => send({ type: 'step-edit', step: index, ...change })
			const mark = played[index]
			const toggle = (optional) => {
				if (optional) step.optional = true
				else delete step.optional
				send({ type: 'optional', step: index, optional })
				renderPanel()
			}
			const box = el('input', { type: 'checkbox', class: 'harvester-optional' })
			box.checked = Boolean(step.optional)
			box.addEventListener('change', () => toggle(box.checked))
			const tool = (text, label, message, usable = true) => {
				const button = el('button', { type: 'button', class: 'harvester-link', text, title: label, 'aria-label': t('{action}: step {number}', { action: label, number: index + 1 }), onclick: () => send({ ...message, step: index }) })
				button.disabled = !usable
				return button
			}
			const tools = el('span', { class: 'harvester-step-tools' },
				tool('↑', t('Move up'), { type: 'step-move', by: -1 }, index > 0),
				tool('↓', t('Move down'), { type: 'step-move', by: 1 }, index < count - 1),
				tool('✕', t('Remove'), { type: 'step-delete' }))
			const details = []
			if (step.action === 'fill') {
				const kind = el('select', { 'aria-label': t('What step {number} fills in', { number: index + 1 }) },
					...[['', t('a value of its own')], ['username', t('the account’s user name')], ['password', t('the account’s password')], ['totp', t('the current one-time code')]]
						.map(([value, text]) => el('option', { value, text })))
				kind.value = step.credential || ''
				kind.addEventListener('change', () => edit({ credential: kind.value }))
				details.push(el('label', {}, `${t('Fills in')} `, kind))
			}
			if ((step.action === 'fill' && !step.credential) || (step.action === 'select' && !Array.isArray(step.value))) {
				const value = el('input', { class: 'harvester-step-value', value: String(step.value || ''), 'aria-label': t('The value of step {number}', { number: index + 1 }) })
				value.addEventListener('change', () => edit({ value: value.value }))
				details.push(value)
			}
			if (step.action === 'select') {
				details.push(el('button', { type: 'button', class: 'harvester-link', text: t('Repeat for each option'), title: t('The steps after this one and the listing are gone through once for each option of the list, its text {choice} in file names and tags', { choice: '{choice}' }), onclick: () => send({ type: 'each', step: index }) }))
			}
			if (step.action === 'each') details.push(eachEditor(step, index))
			if (step.action === 'approve' || step.action === 'handover') details.push(personEditor(step, index))
			if (step.wait && (step.wait.url || step.wait.selector)) {
				const address = step.wait.url
				const shown = step.wait.selector
				const what = address && shown ? t('then waits for an address like {address} and {selector} to show', { address, selector: shown })
					: address ? t('then waits for an address like {address}', { address }) : t('then waits for {selector} to show', { selector: shown })
				details.push(el('small', { class: 'harvester-muted', text: `${what} ` }),
					el('button', { type: 'button', class: 'harvester-link', text: t('wait for the network instead'), onclick: () => edit({ wait: null }) }))
			}
			details.push(timing(step, index, edit))
			const classes = [step.frame && step.frame.length ? 'harvester-framed' : '', mark ? `harvester-step-${mark.state}` : '']
			// The step the test plays now, or the one it failed at (until the next test or edit):
			// clicking that one opens its editor.
			const current = mark && (['playing', 'pausing', 'retrying'].includes(mark.state) || (mark.state === 'failed' && !testing))
			if (current) classes.push('harvester-step-current')
			const item = el('li', { class: classes.filter(Boolean).join(' '), 'data-step': String(index) },
				mark ? stepMark(mark) : '',
				el('span', { text: `${describe(step)} ` }),
				mailCode(step, index),
				phoneCode(step, index),
				// An approval or a handover is never optional: its until says when it is not needed.
				['approve', 'handover'].includes(step.action) ? '' : el('label', { title: t('Best effort: when it fails (not there, not visible), the job waits a few seconds at most and goes on without it') }, box, ` ${t('optional')}`),
				!step.optional && looksLikeConsent(step)
					? el('button', { type: 'button', class: 'harvester-link harvester-hint', text: t('make optional?'), title: t('A consent banner often shows only on the first visit'), onclick: () => toggle(true) })
					: '',
				tools,
				details.length ? el('div', { class: 'harvester-step-details' }, ...details) : '')
			if (mark && mark.state === 'failed' && !testing) {
				item.title = t('The test failed here: click to change its pause or retries')
				item.addEventListener('click', (event) => {
					if (event.target.closest('button, input, select, summary, label, details')) return
					editing = index
					focusEditor = true
					renderPanel()
				})
			}
			return item
		}

		// A step that goes through choices: its fixed values (empty: the options the page offers)
		// and a pattern on the options' texts.
		function eachEditor(step, index) {
			const values = el('input', { class: 'harvester-step-value', value: Array.isArray(step.values) ? step.values.join(', ') : '', placeholder: t('every option'), 'aria-label': t('The values of step {number}, separated by commas', { number: index + 1 }) })
			const pattern = el('input', { class: 'harvester-step-value', value: step.pattern || '', placeholder: t('all of them'), spellcheck: 'false', 'aria-label': t('Only the options whose text matches (a pattern)') })
			const edit = () => send({ type: 'each', step: index, values: values.value.trim() ? values.value.split(',').map((value) => value.trim()).filter(Boolean) : 'options', pattern: pattern.value })
			values.addEventListener('change', edit)
			pattern.addEventListener('change', edit)
			return el('span', {}, el('label', {}, `${t('Values')} `, values), ' ', el('label', {}, `${t('Only options matching')} `, pattern))
		}

		// An approval's or a handover's label, an approval's wait, and the address and element the
		// page shows once the person is through (a handover's optional): typed, this page's address,
		// or an element picked on the page.
		function personEditor(step, index) {
			const edit = (change) => send({ type: 'person-edit', step: index, ...change })
			const until = step.until || {}
			const number = { number: index + 1 }
			const input = (value, label) => el('input', { class: 'harvester-step-value', value: value || '', placeholder: t('none'), spellcheck: 'false', 'aria-label': label })
			const label = input(step.label, t('What you are asked, step {number}', number))
			label.maxLength = 100
			label.addEventListener('change', () => { if (label.value.trim()) edit({ label: label.value.trim() }) })
			const minutes = el('input', { type: 'number', min: '0.5', max: '30', step: '0.5', class: 'harvester-step-number', value: String((step.wait || 600) / 60), 'aria-label': t('How long the job waits for you (minutes), step {number}', number) })
			minutes.addEventListener('change', () => {
				const seconds = Math.round(Number(minutes.value) * 60)
				if (seconds >= 30 && seconds <= 1800) edit({ wait: seconds })
				else minutes.value = String((step.wait || 600) / 60)
			})
			const address = input(until.url, t('The address it waits for (a pattern), step {number}', number))
			address.addEventListener('change', () => edit({ url: address.value }))
			const element = input(until.selector, t('The element it waits for, step {number}', number))
			element.addEventListener('change', () => edit({ selector: element.value }))
			return el('div', { class: 'harvester-person-step' },
				el('label', {}, `${t('Asks')} `, label),
				step.action === 'approve' ? el('label', {}, ` ${t('for up to')} `, minutes, ` ${t('min')}`) : '',
				el('span', { class: 'harvester-muted', text: step.action === 'approve' ? t('Goes on once the page shows:') : t('Not needed once the page shows (optional):') }),
				el('label', {}, `${t('Address')} `, address),
				el('button', { type: 'button', class: 'harvester-link', text: t('this page’s address'), onclick: () => edit({ here: true }) }),
				el('label', {}, `${t('Element')} `, element),
				el('button', { type: 'button', class: 'harvester-link', text: t('Pick an element'), onclick: () => pickUntil(index) }))
		}

		// What the test did with a step: playing (its attempt, when it has more), pausing (a
		// countdown), trying again, passed, skipped (optional), failed, not played (the session).
		function stepMark(mark) {
			const left = mark.ends ? Math.max(0, Math.ceil((mark.ends - Date.now()) / 1000)) : 0
			const attempt = { attempt: mark.attempt, of: mark.of, left }
			const text = {
				playing: mark.of > 1 ? t('▶ playing (attempt {attempt} of {of})', attempt) : t('▶ playing'),
				pausing: t('⏸ pausing {left} s', attempt),
				retrying: t('↻ attempt {attempt} of {of} in {left} s', attempt),
				passed: t('✓ passed'),
				skipped: t('– skipped (optional)'),
				failed: t('✗ failed'),
				session: t('↷ not played (signed in)'),
				stopped: t('■ stopped'),
			}[mark.state] || ''
			return el('span', { class: 'harvester-step-mark', title: mark.why || '', text: `${text} ` })
		}

		// A step's pause before it and its retries: the editor of its timing (closed unless set,
		// or opened from the failed step of a test).
		function timing(step, index, edit) {
			const retry = step.retry || null
			const number = (value, label, max, stepping) => el('input', {
				type: 'number', min: '0', max: String(max), step: stepping, value, class: 'harvester-step-number', 'aria-label': t('{setting} of step {number}', { setting: label, number: index + 1 }),
			})
			const pause = number(step.pause ? String(step.pause) : '', t('The pause before (seconds)'), 60, '0.5')
			pause.placeholder = t('none')
			pause.addEventListener('change', () => {
				const seconds = Number(pause.value)
				if (!pause.value || seconds === 0) {
					delete step.pause
					edit({ pause: null })
				} else if (seconds > 0 && seconds <= 60) {
					step.pause = seconds
					edit({ pause: seconds })
				} else pause.value = step.pause ? String(step.pause) : ''
			})
			const times = number(retry ? String(retry.times) : '0', t('Tries again'), 10, '1')
			const delay = number(String(retry && retry.delay !== undefined ? retry.delay : 2), t('The delay between tries (seconds)'), 60, '0.5')
			const setRetry = () => {
				const count = Number(times.value || 0)
				const seconds = Number(delay.value === '' ? 2 : delay.value)
				if (count === 0) {
					delete step.retry
					edit({ retry: null })
				} else if (Number.isInteger(count) && count >= 1 && count <= 10 && seconds >= 0 && seconds <= 60) {
					step.retry = { times: count, delay: seconds }
					edit({ retry: step.retry })
				} else {
					times.value = retry ? String(retry.times) : '0'
					delay.value = String(retry && retry.delay !== undefined ? retry.delay : 2)
				}
			}
			times.addEventListener('change', setRetry)
			delay.addEventListener('change', setRetry)
			const set = [step.pause ? t('pause {seconds} s', { seconds: step.pause }) : '', retry ? t('retry {times}×', { times: retry.times }) : ''].filter(Boolean).join(', ')
			const box = el('details', { class: 'harvester-step-timing' },
				el('summary', { text: set || t('pause, retry'), title: t('Wait before this step, or try it again when it fails') }),
				el('label', {}, `${t('Pause before it')} `, pause, ' s'),
				el('label', { title: t('When the step fails (its element not there in time, its wait not met), try it again this many times') }, ` ${t('Try again')} `, times, ` ${t('times')}`),
				el('label', {}, `, ${t('every')} `, delay, ' s'))
			box.open = editing === index
			if (editing === index && focusEditor) {
				focusEditor = false
				setTimeout(() => { box.scrollIntoView({ block: 'nearest' }); pause.focus() })
			}
			return box
		}

		// A test's step event: the step's mark (a pause's and a retry's end counted down here).
		function stepEvent(data) {
			if (!testing) return
			played[data.number - 1] = { ...data, ends: data.seconds !== undefined && ['pausing', 'retrying'].includes(data.state) ? Date.now() + data.seconds * 1000 : 0 }
			renderPanel()
		}
		const countdown = setInterval(() => {
			if (!document.body.contains(panel)) clearInterval(countdown)
			for (const node of panel.querySelectorAll('li.harvester-step-pausing, li.harvester-step-retrying')) {
				const mark = played[Number(node.dataset.step)]
				const shown = node.querySelector('.harvester-step-mark')
				if (mark && shown) shown.replaceWith(stepMark(mark))
			}
		}, 500)

		// A code typed into a field (a one-time code's, or digits) may come by e-mail instead: the
		// job then waits for the message in the account's mailbox for codes and types its code.
		function mailCode(step, index) {
			const code = step.action === 'mail-code'
			if (!code && !(step.action === 'fill' && (step.credential === 'totp' || /^\d{4,8}$/.test(String(step.value || ''))))) return ''
			const from = el('input', { class: 'harvester-pattern', value: step.from || '', placeholder: t('Sender, e.g. no-reply@shop\\.example'), 'aria-label': t('Sender (a pattern)') })
			const subject = el('input', { class: 'harvester-pattern', value: step.subject || '', placeholder: t('Subject, e.g. code|Code'), 'aria-label': t('Subject (a pattern)') })
			const use = (on) => {
				if (on) {
					Object.assign(step, { action: 'mail-code', from: from.value, subject: subject.value })
					delete step.credential
					delete step.value
				} else {
					step.action = 'fill'
					step.credential = 'totp'
					for (const key of ['from', 'subject', 'regex', 'wait']) delete step[key]
				}
				send(on ? { type: 'mail-code', step: index, from: from.value, subject: subject.value } : { type: 'mail-code', step: index, off: true })
				renderPanel()
			}
			return el('details', code ? { class: 'harvester-mail-code', open: '' } : { class: 'harvester-mail-code' },
				el('summary', { text: code ? t('code from e-mail') : t('code from e-mail?') }),
				el('small', { class: 'harvester-muted', text: t('The job waits up to 3 minutes for a new message of this sender and subject in the account’s mailbox for codes, and types its code (the first number of 6 to 8 digits).') }),
				field(t('Sender (pattern)'), from), field(t('Subject (pattern)'), subject),
				el('button', { type: 'button', text: code ? t('Change') : t('Use the code from e-mail'), onclick: () => use(true) }),
				code ? el('button', { type: 'button', text: t('Back to a one-time code'), onclick: () => use(false) }) : '')
		}

		// A code typed into a field (a one-time code's, or digits) may come to the phone instead (an
		// SMS, an app the job has no secret of): the job then asks you for it on the jobs page.
		function phoneCode(step, index) {
			const code = step.action === 'ask-code'
			if (!code && !(step.action === 'fill' && (step.credential === 'totp' || /^\d{4,8}$/.test(String(step.value || ''))))) return ''
			const label = el('input', { class: 'harvester-step-value', value: step.label || '', placeholder: t('SMS code'), maxlength: '100', 'aria-label': t('Where the code comes from') })
			const use = (on) => {
				if (on) {
					Object.assign(step, { action: 'ask-code', label: label.value.trim() || t('SMS code') })
					delete step.credential
					delete step.value
				} else {
					step.action = 'fill'
					step.credential = 'totp'
					delete step.label
				}
				send(on ? { type: 'ask-code', step: index, label: step.label } : { type: 'ask-code', step: index, off: true })
				renderPanel()
			}
			return el('details', code ? { class: 'harvester-mail-code', open: '' } : { class: 'harvester-mail-code' },
				el('summary', { text: code ? t('code from the phone') : t('code from the phone?') }),
				el('small', { class: 'harvester-muted', text: t('When the job comes to this step, it shows a field for the code on the jobs page and notifies you, then waits up to 10 minutes for you to type the code you got (an SMS, a call, an app).') }),
				field(t('Where the code comes from'), label),
				el('button', { type: 'button', text: code ? t('Change') : t('Ask me for the code'), onclick: () => use(true) }),
				code ? el('button', { type: 'button', text: t('Back to a one-time code'), onclick: () => use(false) }) : '')
		}

		// What a step of the test asks you now (a code from the phone), until its next step event.
		let asking = null
		function askingLine() {
			if (!testing || !asking) return ''
			if (asking.kind !== 'code') return el('p', { class: 'harvester-asking', text: t('{label}: approve it on your phone or in the app. The job goes on once the site shows it went through.', { label: asking.label }) })
			const input = el('input', { class: 'harvester-code', autocomplete: 'one-time-code', spellcheck: 'false', maxlength: '64', 'aria-label': t('Enter the code from {label}', { label: asking.label }) })
			return el('form', {
				class: 'harvester-asking',
				onsubmit: (event) => {
					event.preventDefault()
					if (!input.value.trim()) return
					send({ type: 'test-code', code: input.value.trim() })
					asking = null
					renderPanel()
				},
			}, el('label', {}, el('span', { text: t('Enter the code from {label}', { label: asking.label }) }), input),
			el('button', { class: 'primary', type: 'submit', text: t('Send') }))
		}

		function renderPanel() {
			test.textContent = testing ? t('Stop the test') : t('Test the recipe')
			testJob.hidden = testing
			panel.replaceChildren(
				el('div', { class: 'harvester-level-head' }, el('h3', { text: t('Steps') })),
				leaks,
				warnings.length ? el('ul', { class: 'harvester-error harvester-warnings' }, ...warnings.map((warning) => el('li', { text: warning }))) : '',
				el('ol', {}, ...(recipe.steps || []).map((step, index, all) => renderStep(step, index, all.length))),
				el('div', { class: 'harvester-level-head' }, el('h3', { text: t('Listing') }),
					el('button', { type: 'button', text: t('Undo pick'), title: t('Take back the last change of the picks'), onclick: () => send({ type: 'undo-pick' }) })),
				el('p', { text: recipe.start || ((recipe.steps || []).length ? t('(where the steps end)') : t('(the page where you pick)')) }),
				...(recipe.follow || []).map((spec, level) =>
					renderLevel(t('Level {level}: links to follow', { level: level + 1 }), spec, proposals[`follow${level}`], send, 'follow', level, suggestions, repick)),
				recipe.links && (recipe.links.selector || recipe.links.kind === 'json')
					? renderLevel(t('File links'), recipe.links, proposals.links, send, 'links', 0, suggestions, repick)
					: el('p', { text: t('File links: not picked yet.') }),
				recipe.next && recipe.next.selector
					? renderLevel(t('Next page'), recipe.next, proposals.next, send, 'next', Number(recipe.next.level || 0), suggestions, repick)
					: el('p', { text: t('Next page: none.') }),
				testing ? el('p', { class: 'harvester-muted', 'aria-live': 'polite', text: t('Testing: the browser shows the job’s way through the site…') }) : '',
				askingLine(),
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

		// The recorder starts with the form's recipe, to change its steps and picks there; with steps,
		// in Browse (recording asks where the new ones go).
		socket.addEventListener('open', () => {
			send({ type: 'load', recipe })
			if (!placed) send({ type: 'mode', mode: 'browse' })
		})
		socket.addEventListener('message', (event) => {
			if (typeof event.data !== 'string') return // a frame: the screen shows it (wireScreen)
			const data = JSON.parse(event.data)
			if (cookies.handle(data)) return
			if (data.type === 'frame') { if (!left(screen, socket)) screen.frame(data.data) }
			else if (data.type === 'cursor') screen.style.cursor = data.cursor
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') { message.textContent = shown(data.message, data.message_l10n); screen.say(message.textContent) }
			else if (data.type === 'viewport') screen.resized(data)
			else if (data.type === 'network') network.show(data)
			else if (data.type === 'test-started') { testing = true; tested = null; played = {}; editing = null; playedShape = shape(); renderPanel() }
			else if (data.type === 'test-step') { asking = null; stepEvent(data) }
			else if (data.type === 'test-asking') { asking = data; renderPanel() }
			else if (data.type === 'until-picked' && beforeUntil) { const back = beforeUntil; beforeUntil = null; choose(back) }
			else if (data.type === 'test') {
				testing = false
				tested = data.result
				// A step still under way when the test ended was cut short by the stop, or by the end.
				for (const [index, mark] of Object.entries(played)) {
					if (!['playing', 'pausing', 'retrying'].includes(mark.state)) continue
					if (data.result.ended === 'stopped') mark.state = 'stopped'
					else delete played[index]
				}
				renderPanel()
				// The outcome where it is seen: the drawer opened in a full window, the panel scrolled.
				container.classList.add('harvester-drawer-open')
				const result = panel.querySelector('.harvester-test')
				if (result) result.scrollIntoView({ block: 'nearest' })
			}
			else if (data.type === 'where') showWhere(data.frame)
			else if (data.type === 'warning') leaks.append(el('p', { text: shown(data.message, data.message_l10n) }))
			else if (data.type === 'requests') { requests = data.items; if (!networkBox.hidden) renderRequests() }
			else if (data.type === 'request-body') { if (previews.has(data.id)) { previews.set(data.id, data); renderRequests() } }
			else if (data.type === 'recipe') {
				// The steps and picks are the recorder's (it started with the form's).
				recipe = { ...recipe, ...data.recipe }
				if (!data.recipe.next) delete recipe.next
				if (shape() !== playedShape) { played = {}; editing = null }
				proposals = data.proposals
				warnings = (data.warnings || []).map((warning, index) => shown(warning, (data.warnings_l10n || [])[index]))
				message.textContent = ''
				renderPanel()
			}
		})
		socket.addEventListener('close', () => { message.textContent ||= t('The recorder’s browser closed.'); screen.say(message.textContent) })

		const box = wireScreen(screen, send, socket)
		const container = el('div', { class: 'harvester-recorder' })
		const drawer = el('button', {
			class: 'harvester-drawer-switch',
			text: t('Recipe'),
			title: t('Show or hide the steps and picks (in a full window)'),
			onclick: () => container.classList.toggle('harvester-drawer-open'),
		})
		container.append(
			el('h2', { text: t('Recorder') }),
			el('p', { text: t('Sign in and go to the listing as you would (passwords are not recorded: the job uses its account). Then pick the links to follow (if the files are a level deeper) and the file links: their patterns are proposed, the matches outlined. Test the recipe to see what the job would find.') }),
			overlay(
				el('div', { class: 'harvester-toolbar' },
					...navigation(send),
					url,
					el('button', { type: 'button', text: t('Go'), onclick: () => send({ type: 'goto', url: url.value }) }),
					el('button', { type: 'button', text: t('Undo step'), onclick: () => send({ type: 'undo' }) }),
					el('button', { type: 'button', text: t('Clear picks'), onclick: () => { recipe = { steps: recipe.steps }; send({ type: 'clear' }) } }),
					fullWindow(container, box, send),
					cookies.button,
					networkButton,
					drawer,
					test,
					testJob,
					// Done and Cancel together at the end, never one of them alone on a line.
					el('span', { class: 'harvester-toolbar-end' },
						el('button', { class: 'primary', text: t('Done'), onclick: () => close(true) }),
						el('button', { text: t('Cancel'), onclick: () => close(false) }))),
				modes, placing, el('div', { class: 'harvester-toolbar' }, modeHelp, scroll, approve, handover), where, network, message, cookies.box, networkBox),
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
				text: label ? said(label) : (value === '' ? t('(empty)') : value),
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
		return el('div', { class: 'harvester-values harvester-suggestions' }, el('small', { class: 'harvester-muted', text: `${t('Pick:')} ` }), ...chips)
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
					verdict.textContent = t('✓ a valid pattern')
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
				verdict.textContent = bad ? t('✗ an address starting with https://') : ''
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
			return el('div', { class: 'harvester-values' }, el('small', { class: 'harvester-muted', text: `${t('Insert:')} ` }),
				...option.values.map((name) => el('button', { type: 'button', class: 'harvester-chip', text: `{${name}}`, onclick: () => insert(name) })))
		}
		return null
	}

	// The folded groups of a job's options (an option's group, from the server).
	const GROUPS = () => ({
		browser: t('Browser and connection'),
		files: t('Files and sizes'),
		runs: t('Repeated runs'),
		links: t('Linked files'),
	})

	// An option's title: "Through the proxy" in the admin's name for it, the others as the
	// source gives them.
	const optionTitle = (key, option) => (key === 'home' ? t('Through {name}', { name: proxyName(option.proxy) }) : said(option.title))

	// The job's form; with sent (an address from ?fetch=), the new job fetches it once saved.
	function renderForm(root, job, prefill, sent) {
		const value = job || prefill || BLANK
		// The job saved so far: a new one is, after its first Save, saved again with PUT.
		let current = job
		// The zone its schedule runs in: a job keeps its own, a new one takes the person's.
		const zone = job ? job.timezone || 'UTC' : ZONE
		const name = el('input', { value: value.name, required: '' })
		// Required: a job made from a recipe file comes without one (the person chooses it).
		const target = el('input', { value: value.target, required: '' })
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
						scheduleHint.textContent = shown(answer.error, answer.error_l10n)
						return
					}
					const format = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
					const next = answer.next.map((at) => new Date(at * 1000).toLocaleString(undefined, format)).join(' · ')
					scheduleHint.replaceChildren(el('strong', { text: shown(answer.description, answer.description_l10n) }), el('br'),
						el('span', { class: 'harvester-muted', text: t('Next runs (your time): {next}', { next }) }))
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
			if (suggestions.tags) tags.value = [...new Set([...tags.value.split(',').map((tag) => tag.trim()).filter(Boolean), ...suggestions.tags])].join(', ')
		}

		options.append(el('legend', { text: t('How the files are fetched') }))
		// The source's essentials first; the other options in folded groups, each opened when one
		// of its options differs from its default and saying how many do.
		const groups = new Map()
		const differs = (key) => {
			const option = chosen.options[key]
			const now = optionInputs[key].value
			return typeof option.default === 'number' ? Number(now) !== option.default : String(now) !== String(option.default)
		}
		const groupOf = (name) => {
			if (!name) return options
			if (!groups.has(name)) {
				const changed = el('span', { class: 'harvester-muted' })
				const box = el('details', { class: 'harvester-group', 'data-group': name },
					el('summary', {}, el('span', { text: GROUPS()[name] || name }), changed))
				box.keys = []
				box.count = () => {
					const count = box.keys.filter(differs).length
					changed.textContent = count ? ` ${n('(%n changed)', '(%n changed)', count)}` : ''
					return count
				}
				box.addEventListener('input', box.count)
				box.addEventListener('change', box.count)
				groups.set(name, box)
			}
			return groups.get(name)
		}
		for (const [key, option] of Object.entries(chosen.options)) {
			// A saved job without the option (saved before it existed) has its value from before.
			const current = key in value.options ? value.options[key] : value.id && option.before != null ? option.before : option.default
			const into = groupOf(option.group)
			if (into !== options) into.keys.push(key)
			if (option.kind === 'recipe') {
				const textarea = el('textarea', { rows: '10', class: 'harvester-recipe' })
				textarea.value = current
				optionInputs[key] = textarea
				const leaks = el('div', { class: 'harvester-warning', 'aria-live': 'polite' })
				const checkLeaks = () => {
					let parsed = {}
					try {
						parsed = JSON.parse(textarea.value || '{}')
					} catch (e) {}
					leaks.replaceChildren(...leakedPasswords(parsed).map((text) => el('p', { text })))
				}
				textarea.addEventListener('input', checkLeaks)
				checkLeaks()
				into.append(field(said(option.title), textarea, said(option.description)), leaks,
					el('button', {
						type: 'button',
						text: t('Record in the browser'),
						onclick: () => openRecorder(root, page, textarea, applySuggestions, () => Object.fromEntries(
							['path', 'download_url', 'max_pages', 'browser', 'http1', 'file_type', 'min_size', 'max_size', 'wait_pattern']
								.filter((name) => optionInputs[name]).map((name) => [name, optionInputs[name].value])), () => (current && current.id) || null),
					}))
			} else {
				if (option.kind === 'flag') {
					// On or off, kept as 1 or 0 (as the jobs saved before the checkbox).
					const box = el('input', { type: 'checkbox', 'data-option': key })
					box.checked = Number(current) === 1
					optionInputs[key] = { get value() { return box.checked ? 1 : 0 } }
					into.append(el('label', { class: 'harvester-check' }, box,
						el('span', {}, el('strong', { text: optionTitle(key, option) }), el('br'), el('small', { class: 'harvester-muted', text: said(option.description) }))))
					continue
				}
				if (option.kind === 'job') {
					// One of the person's on-demand website jobs, kept as its ID (0: none); the list
					// comes as the jobs' list does, a job no longer there is none.
					const select = el('select', { 'data-option': key }, el('option', { value: '0', text: t('None') }))
					// Until the list is there, the job keeps what it had.
					let listed = false
					api('GET', '/jobs?brief=1').then((jobs) => {
						select.append(...jobs.filter((other) => onDemand(other) && other.source === 'site')
							.map((other) => el('option', { value: String(other.id), text: `${other.name} (${other.target})` })))
						select.value = String(current || 0)
						if (!select.value) select.value = '0'
						listed = true
					}).catch(() => {})
					optionInputs[key] = { get value() { return Number(listed ? select.value : current) || 0 } }
					into.append(field(optionTitle(key, option), select, said(option.description)))
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
					into.append(field(optionTitle(key, option), select, said(option.description)))
					continue
				}
				optionInputs[key] = optionInput(option, current)
				into.append(field(optionTitle(key, option), optionInputs[key], said(option.description)))
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
		for (const box of groups.values()) {
			box.open = box.count() > 0
			options.append(box)
		}

		const save = el('button', { class: 'primary', type: 'submit', form: 'harvester-job-form', text: t('Save') })
		// The job as the form holds it, as Save sends it (and Export recipe, Save as template).
		const formBody = () => ({
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
		})
		const heading = el('span', { text: job ? t('Edit {name}', { name: job.name }) : t('New job: {source}', { source: said(chosen.title) }) })
		const form = el('form', {
			id: 'harvester-job-form',
			class: 'harvester-form',
			onsubmit: async (event) => {
				event.preventDefault()
				if (save.disabled) return
				const body = formBody()
				save.disabled = true
				error.textContent = ''
				try {
					current = await api(current ? 'PUT' : 'POST', current ? `/jobs/${current.id}` : '/jobs', body)
					heading.textContent = t('Edit {name}', { name: current.name })
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
					error.textContent = t('The job is saved, its account is not: {error}', { error: e.message })
					save.disabled = false
					return
				}
				// Made from an address sent to Harvester: it fetches that address now.
				if (sent && onDemand(current)) {
					try {
						await sendTo(current, sent)
					} catch (e) {
						notices[current.id] = t('The address is not fetched: {error}', { error: e.message })
					}
				}
				show(root)
			},
		},
		el('h2', { class: 'harvester-with-icon' }, icon(chosen.name), heading),
		field(t('Name'), name),
		field(t('Folder'), target, t('In your files, e.g. /Magazines/iX.')),
		chosen.address ? el('label', { class: 'harvester-check' }, onDemandBox,
			el('span', {}, el('strong', { text: t('On demand') }), el('br'),
				el('small', { class: 'harvester-muted', text: t('No schedule: it runs when you send it an address (Fetch a URL…, or Send to Harvester from any page), in place of its own, which may stay empty.') }))) : '',
		el('div', { class: 'harvester-scheduled' },
			field(t('Schedule'), schedule, zone === ZONE
				? t('Cron: minute hour day month weekday, in your time zone ({zone}). 0 6 * * * is daily at 06:00.', { zone })
				: t('Cron: minute hour day month weekday, in the job’s time zone ({zone}). 0 6 * * * is daily at 06:00.', { zone })),
			scheduleHint),
		el('label', { class: 'harvester-check harvester-once' }, once,
			el('span', { text: t('Delete the job after a run that went through (its files stay; one that failed keeps it, to say why)') })),
		field(t('Tags'), tags, t('Comma-separated; may use the link’s values, e.g. {year}. A tag whose value a file lacks is left out.', { year: '{year}' })),
		el('label', { class: 'harvester-check harvester-scheduled' }, enabled, el('span', { text: t('Enabled: runs on its schedule') })),
		options)
		// A schedule's fields, or (on demand) whether it goes after its run.
		const showMode = () => {
			for (const part of form.querySelectorAll('.harvester-scheduled')) part.hidden = demanded()
			form.querySelector('.harvester-once').hidden = !demanded()
		}
		onDemandBox.addEventListener('change', showMode)
		showMode()
		// Below the field, now that it is in its label.
		schedule.after(suggestionChips(schedule, SCHEDULES()))
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
			text: t('Back to the jobs'),
			onclick: () => {
				if (!credentials.typed() || confirm(t('Leave without saving the account you typed?'))) show(root)
			},
		})
		// The recipe as a file to keep or share, or as a template of the person's own: the server
		// checks the form as Save does, and leaves out the account, session and folder.
		const notice = el('p', { class: 'harvester-muted', role: 'status' })
		const exportButton = el('button', {
			type: 'button',
			text: t('Export recipe'),
			title: t('Download the job’s recipe and settings as a file (without its account, session and folder)'),
			onclick: async () => {
				error.textContent = ''
				notice.textContent = ''
				try {
					const file = await api('POST', '/recipes/export', { ...formBody(), target: target.value || '/' })
					download(`${(file.name || 'recipe').replace(/[\\/:*?"<>|]+/g, '-')}.harvester.json`, JSON.stringify(file, null, 2))
					notice.textContent = t('Exported: without the account, session and folder.')
				} catch (e) {
					error.textContent = e.message
				}
			},
		})
		const templateButton = el('button', {
			type: 'button',
			text: t('Save as template'),
			title: t('Keep this job as a template of your own on the New job page (without its account, session and folder)'),
			onclick: async () => {
				error.textContent = ''
				notice.textContent = ''
				try {
					await api('POST', '/templates', { ...formBody(), target: target.value || '/' })
					notice.textContent = t('Saved as your template: New job lists it.')
				} catch (e) {
					error.textContent = e.message
				}
			},
		})
		page = el('div', {}, form, credentials, el('div', { class: 'harvester-form-actions' }, error, save, back, exportButton, templateButton, notice))
		root.replaceChildren(page)
		name.focus()
		renderCredentials(credentials, job, chosen)
	}

	async function show(root, known = {}) {
		try {
			await renderJobs(root, known)
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
		#harvester .harvester-steps .harvester-step-tools { margin-inline-start: 6px; white-space: nowrap; }
		#harvester .harvester-steps .harvester-step-tools button { min-height: 0; padding: 0 4px; }
		#harvester .harvester-steps .harvester-step-details { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; margin: 2px 0 6px; }
		#harvester .harvester-person-step { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; }
		#harvester .harvester-steps .harvester-step-details select, #harvester .harvester-steps .harvester-step-value { min-height: 0; margin: 0; }
		#harvester .harvester-steps .harvester-step-number { width: 5em; min-height: 0; margin: 0 2px; }
		#harvester .harvester-steps .harvester-step-timing summary { cursor: pointer; color: var(--color-text-maxcontrast); }
		#harvester .harvester-steps .harvester-step-timing label { white-space: nowrap; }
		#harvester .harvester-steps li.harvester-step-current { outline: 2px solid var(--color-primary-element); outline-offset: 2px; border-radius: var(--border-radius); background: var(--color-primary-element-light, var(--color-background-hover)); }
		#harvester .harvester-steps li.harvester-step-failed.harvester-step-current { outline-color: var(--color-error); cursor: pointer; }
		#harvester .harvester-steps .harvester-step-mark { font-weight: bold; white-space: nowrap; }
		#harvester .harvester-steps .harvester-step-passed .harvester-step-mark { color: var(--color-success-text, var(--color-success)); }
		#harvester .harvester-steps .harvester-step-failed .harvester-step-mark { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-steps .harvester-step-skipped .harvester-step-mark, #harvester .harvester-steps .harvester-step-session .harvester-step-mark { color: var(--color-text-maxcontrast); }
		#harvester .harvester-placing:not(:empty) { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin: 4px 0; }
		#harvester .harvester-warning p { margin: 4px 0; padding: 4px 8px; border-inline-start: 3px solid var(--color-warning); color: var(--color-warning-text, var(--color-main-text)); }
		#harvester .harvester-where { display: block; color: var(--color-text-maxcontrast); }
		#harvester .harvester-cookies-panel { max-height: 50vh; overflow: auto; padding: 8px; border: 1px solid var(--color-border); border-radius: var(--border-radius); background: var(--color-main-background); }
		#harvester .harvester-cookies { border-collapse: collapse; overflow-wrap: anywhere; }
		#harvester .harvester-cookies th, #harvester .harvester-cookies td { padding: 2px 6px; text-align: left; vertical-align: top; }
		#harvester .harvester-cookie-value { white-space: pre-wrap; }
		#harvester .harvester-network { margin: 4px 0; }
		#harvester .harvester-json-body { width: 100%; font-family: monospace; font-size: 12px; }
		#harvester .harvester-preview { max-height: 320px; overflow: auto; margin: 6px 0; padding: 8px; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--color-background-dark); border-radius: var(--border-radius); }
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
		/* A state's colour as a light tint of it behind its text colour, readable in both themes
		   (the text colours are made for the page's background, not for the full colour). */
		#harvester .harvester-status-ok { background: color-mix(in srgb, var(--color-success) 15%, transparent); color: var(--color-success-text, var(--color-main-text)); }
		#harvester .harvester-status-failed, #harvester .harvester-status-auth { background: color-mix(in srgb, var(--color-error) 15%, transparent); color: var(--color-error-text, var(--color-main-text)); }
		#harvester .harvester-status-changed, #harvester .harvester-status-held, #harvester .harvester-status-challenge, #harvester .harvester-status-asking { background: color-mix(in srgb, var(--color-warning) 15%, transparent); color: var(--color-warning-text, var(--color-main-text)); }
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
		#harvester .harvester-log-warning .harvester-log-text { color: var(--color-warning-text, var(--color-warning)); font-weight: bold; }
		#harvester .harvester-log-done .harvester-log-text { color: var(--color-success-text, var(--color-success)); font-weight: bold; }
		#harvester .harvester-status-running { background: var(--color-primary-element); color: var(--color-primary-element-text); }
		#harvester .harvester-status-queued { background: var(--color-primary-element-light); color: var(--color-primary-element-light-text, var(--color-main-text)); }
		#harvester .harvester-asking { display: flex; flex-wrap: wrap; align-items: end; gap: 6px; margin-top: 4px; }
		#harvester .harvester-asking label { display: flex; flex-direction: column; }
		#harvester .harvester-asking small { flex-basis: 100%; }
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
		/* Save and the others stay in sight at the window's foot while the form scrolls. */
		#harvester .harvester-form-actions { position: sticky; bottom: calc(var(--default-grid-baseline, 4px) * -6); z-index: 2; margin-top: 16px; padding: 4px 0 12px; border-top: 1px solid var(--color-border); background: var(--color-main-background); }
		#harvester .harvester-group { margin: 12px 0; border-top: 1px solid var(--color-border); padding-top: 8px; }
		#harvester .harvester-group > summary { cursor: pointer; font-weight: bold; padding: 4px 0; }
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
		/* Read by screen readers, not shown. */
		#harvester .harvester-hidden { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
		#harvester .harvester-wizard h3 { margin-top: 0; }
		#harvester .harvester-wizard-help { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--color-text-maxcontrast); }
		#harvester .harvester-wizard pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 8px 12px; background: var(--color-background-dark); border-radius: var(--border-radius); }
		#harvester .harvester-wizard button { margin: 8px 8px 0 0; }
		#harvester .harvester-kinds { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-bottom: 8px; }
		#harvester .harvester-kinds .harvester-card { margin: 0; max-width: none; display: flex; flex-direction: column; }
		#harvester .harvester-kinds .harvester-card p { flex: 1; }
		#harvester .harvester-card { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 12px 16px; margin: 12px 0; max-width: 760px; }
		#harvester .harvester-recipe, #harvester .harvester-pattern { font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-toolbar { display: flex; flex-wrap: wrap; gap: 4px; margin: 8px 0; align-items: center; }
		#harvester .harvester-toolbar button { margin: 0; }
		#harvester .harvester-toolbar-end { display: flex; flex-wrap: nowrap; gap: 4px; margin-inline-start: auto; }
		#harvester .harvester-url { flex: 1 1 14em; min-width: 10em; margin: 0; }
		#harvester .harvester-test-summary { margin: 4px 0 8px; padding: 4px 10px; border-radius: var(--border-radius); font-weight: bold; }
		#harvester .harvester-test-ok { background: color-mix(in srgb, var(--color-success) 15%, transparent); color: var(--color-success-text, var(--color-main-text)); }
		#harvester .harvester-test-failed { background: color-mix(in srgb, var(--color-error) 15%, transparent); color: var(--color-error-text, var(--color-main-text)); }
		#harvester .harvester-test-empty { background: color-mix(in srgb, var(--color-warning) 15%, transparent); color: var(--color-warning-text, var(--color-main-text)); }
		#harvester .harvester-test-stopped { background: var(--color-background-dark); }
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
		#harvester .harvester-keys-hint { position: absolute; left: 8px; bottom: 8px; padding: 2px 8px; border-radius: var(--border-radius); background: var(--color-main-background); color: var(--color-main-text); box-shadow: 0 1px 4px var(--color-box-shadow); opacity: 0; pointer-events: none; }
		#harvester .harvester-screen-box:focus-within .harvester-keys-hint { opacity: 0.92; }
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
		#harvester .harvester-click { display: block; margin: 4px 0; }
		#harvester .harvester-test-pages { padding-inline-start: 20px; }
		#harvester .harvester-shot-image { display: block; max-width: 100%; height: auto; border: 1px solid var(--color-border); border-radius: var(--border-radius); }
		#harvester .harvester-link { border: none; background: none; min-height: 0; padding: 0 0 0 8px; color: var(--color-primary-element); text-decoration: underline; cursor: pointer; }
		/* A narrow window: the recorder's screen above its steps, at the full width. */
		@media (max-width: 800px) {
			#harvester .harvester-stage { flex-direction: column; align-items: stretch; }
			#harvester .harvester-recorder:not(.harvester-wide) .harvester-screen-box { flex: none; max-width: none; }
			#harvester .harvester-recorder:not(.harvester-wide) .harvester-steps { min-width: 0; max-height: none; }
		}
		/* A phone: the jobs as cards, a header's parts one below the other, the log's lines wrapped. */
		@media (max-width: 700px) {
			#harvester { padding: 16px 12px; }
			#harvester .harvester-header { flex-direction: column; align-items: stretch; gap: 8px; }
			#harvester .harvester-jobs thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
			#harvester .harvester-jobs, #harvester .harvester-jobs tbody, #harvester .harvester-jobs tr, #harvester .harvester-jobs td { display: block; }
			#harvester .harvester-jobs tr { padding: 10px 0; border-bottom: 1px solid var(--color-border); }
			#harvester .harvester-jobs td { padding: 2px 0; border: 0; }
			#harvester .harvester-jobs td:empty { display: none; }
			#harvester .harvester-jobs td[data-label]::before { content: attr(data-label) ": "; color: var(--color-text-maxcontrast); }
			#harvester .harvester-jobs td.harvester-with-icon { display: flex; white-space: normal; }
			#harvester .harvester-jobs td.harvester-with-icon .harvester-icon { margin: 0; }
			#harvester .harvester-jobs td code { white-space: normal; overflow-wrap: anywhere; }
			#harvester .harvester-jobs .harvester-actions { display: flex; flex-wrap: wrap; gap: 4px; padding-top: 6px; text-align: start; }
			#harvester .harvester-jobs .harvester-actions button { margin: 0; }
			#harvester .harvester-message { max-width: none; }
			#harvester .harvester-fetch { flex-wrap: wrap; }
			#harvester .harvester-log { padding: 8px; }
			#harvester .harvester-log li { flex-wrap: wrap; gap: 0 8px; padding: 2px 0; }
			#harvester .harvester-log-text { flex-basis: 100%; }
		}
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
