// The app's page in Nextcloud: the user's jobs, a job's form (blank or from a template) with
// its account, and the recorder (the app's browser, shown and driven from here), which proposes
// the links' patterns and shows what they match. Plain JavaScript, no build: Nextcloud loads
// it into the page AppAPI renders for the top menu.
(function () {
	'use strict'

	const API = OC.generateUrl('/apps/app_api/proxy/harvester/api')
	// The recorder's WebSocket goes to HaRP directly (AppAPI's PHP proxy has none).
	const RECORDER = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${OC.webroot || ''}/exapps/harvester/ws/record`
	const VIEWPORT = { width: 1280, height: 800 }
	const STATUS = { ok: '✔', failed: '✖', auth: '🔒' }
	const BLANK = { name: '', source: 'site', target: '/Harvester', schedule: '0 6 * * *', options: {}, tags: [], enabled: true }
	let sources = []
	let presets = []

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

	function when(timestamp) {
		return timestamp ? new Date(timestamp * 1000).toLocaleString() : 'never'
	}

	function describe(step) {
		if (step.action === 'goto') return `Open ${step.url}`
		if (step.action === 'click') return `Click ${step.text ? `“${step.text}”` : step.selector}`
		if (step.action === 'press') return `Press ${step.key} in ${step.selector}`
		if (step.credential) return `Fill in the account's ${step.credential} (${step.selector})`
		return `Fill in “${step.value}” (${step.selector})`
	}

	async function renderJobs(root) {
		const jobs = await api('GET', '/jobs')
		const table = el('table', { class: 'harvester-jobs' },
			el('tr', {}, ...['Job', 'Folder', 'Schedule', 'Tags', 'Last run', ''].map((h) => el('th', { text: h }))))
		for (const job of jobs) {
			const status = job.running ? '⏳ running' : `${STATUS[job.last_status] || ''} ${job.last_message || ''}`
			table.append(el('tr', {},
				el('td', { text: job.enabled ? job.name : `${job.name} (paused)` }),
				el('td', { text: job.target }),
				el('td', { text: job.schedule }),
				el('td', { text: job.tags.join(', ') }),
				el('td', { text: `${when(job.last_run)} ${status}` }),
				el('td', {},
					el('button', { text: 'Run now', onclick: async () => { await api('POST', `/jobs/${job.id}/run`); show(root) } }),
					el('button', { text: 'Edit', onclick: () => renderForm(root, job) }),
					el('button', {
						text: 'Delete',
						onclick: async () => {
							if (confirm(`Delete ${job.name}? The files it downloaded stay.`)) {
								await api('DELETE', `/jobs/${job.id}`)
								show(root)
							}
						},
					}))))
		}
		root.replaceChildren(
			el('h2', { text: 'Harvester' }),
			el('p', { text: 'Fetches files from websites into your folders on a schedule, and tags them.' }),
			jobs.length ? table : el('p', { text: 'No jobs yet.' }),
			el('button', { class: 'primary', text: 'New job', onclick: () => renderStart(root) }))
	}

	// A new job: blank (recorded in the browser) or from a template with its parameters.
	function renderStart(root) {
		const error = el('p', { class: 'harvester-error' })
		const cards = presets.map((preset) => {
			const inputs = {}
			const fields = Object.entries(preset.parameters).map(([name, spec]) => {
				inputs[name] = el('input', { value: spec.default })
				return field(spec.title, inputs[name], spec.description)
			})
			return el('div', { class: 'harvester-card' },
				el('h3', { text: preset.title }), el('p', { text: preset.description }), ...fields,
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
		root.replaceChildren(
			el('h2', { text: 'New job' }),
			el('div', { class: 'harvester-card' },
				el('h3', { text: 'Any website' }),
				el('p', { text: 'Record it in the browser: sign in if need be, then pick the links to follow and the file links; the app proposes their patterns.' }),
				el('button', { class: 'primary', text: 'Start blank', onclick: () => renderForm(root, null) })),
			...cards, error,
			el('button', { text: 'Back to the jobs', onclick: () => show(root) }))
	}

	async function renderCredentials(container, job, source) {
		container.replaceChildren()
		if (!source.credentials.length) return
		container.append(el('h3', { text: 'The account this job signs in with' }),
			el('p', { text: 'Filled into the recipe’s username and password fields; never shown again.' }))
		if (!job) {
			container.append(el('p', { text: 'Save the job first, then set its account here.' }))
			return
		}
		const stored = await api('GET', `/jobs/${job.id}/credentials`)
		const inputs = {}
		for (const name of source.credentials) {
			inputs[name] = el('input', {
				type: name === 'password' ? 'password' : 'text',
				placeholder: stored[name] ? '(stored, leave empty to keep)' : '',
				autocomplete: 'off',
			})
			container.append(field(name, inputs[name]))
		}
		container.append(el('button', {
			text: 'Save account',
			onclick: async (event) => {
				event.preventDefault()
				const body = Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value]))
				await api('PUT', `/jobs/${job.id}/credentials`, body)
				renderCredentials(container, job, source)
			},
		}))
	}

	// One picked level in the recorder's panel: its pattern (editable), what it matches on the
	// page, examples with their values, and the proposed file name and tags.
	function renderLevel(title, spec, proposal, send, target, level, suggestions) {
		const pattern = el('input', { class: 'harvester-pattern', value: spec.pattern || '' })
		pattern.addEventListener('change', () => send({ type: 'pattern', target, level, pattern: pattern.value }))
		const box = el('div', { class: 'harvester-level' }, el('h4', { text: title }),
			el('small', { text: spec.selector }), field('Pattern (named groups become values)', pattern))
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

	// The recorder: the browser's screen, the person's mouse and keys to it, and the recipe so far.
	function openRecorder(root, page, textarea, apply) {
		let recipe = {}
		try {
			recipe = JSON.parse(textarea.value || '{}')
		} catch (e) {}
		let proposals = {}
		const suggestions = {}
		const socket = new WebSocket(RECORDER)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The recorder’s browser' })
		const url = el('input', { class: 'harvester-url', placeholder: 'https://… the site’s first page' })
		const message = el('p', { class: 'harvester-error' })
		const panel = el('div', { class: 'harvester-steps' })
		const modes = el('div', { class: 'harvester-modes' })
		const help = {
			record: 'Use the site as usual: sign in and go to the listing. Clicks, typing and pages opened here are recorded.',
			browse: 'Go anywhere without recording (into a year, an issue, …) to pick there.',
			follow: 'Click one link that leads to a further listing (a year, a category): similar links are picked with it.',
			links: 'Click one file link: the similar ones are picked with it, and a pattern is proposed.',
			next: 'Click the link to the next page of this listing (if there is one).',
		}
		const modeHelp = el('small', { text: help.record })
		const labels = [['record', 'Record steps'], ['browse', 'Browse'], ['follow', 'Pick links to follow'], ['links', 'Pick file links'], ['next', 'Pick next-page link']]
		for (const [mode, label] of labels) {
			const radio = el('input', { type: 'radio', name: 'harvester-mode', value: mode })
			radio.checked = mode === 'record'
			radio.addEventListener('change', () => { send({ type: 'mode', mode }); modeHelp.textContent = help[mode]; screen.focus() })
			modes.append(el('label', {}, radio, ` ${label} `))
		}

		function renderPanel() {
			panel.replaceChildren(
				el('h3', { text: 'Steps' }),
				el('ol', {}, ...(recipe.steps || []).map((step) => el('li', { text: describe(step) }))),
				el('h3', { text: 'Listing' }),
				el('p', { text: recipe.start || '(the page where you pick)' }),
				...(recipe.follow || []).map((spec, level) =>
					renderLevel(`Level ${level + 1}: links to follow`, spec, proposals[`follow${level}`], send, 'follow', level, suggestions)),
				recipe.links && recipe.links.selector
					? renderLevel('File links', recipe.links, proposals.links, send, 'links', 0, suggestions)
					: el('p', { text: 'File links: not picked yet.' }),
				el('p', { text: `Next page: ${recipe.next ? `${recipe.next.selector} (level ${recipe.next.level})` : 'none'}` }))
		}

		function close(keep) {
			socket.close()
			if (keep) {
				textarea.value = JSON.stringify(recipe, null, 2)
				apply(suggestions)
			}
			root.replaceChildren(page)
		}

		socket.addEventListener('message', (event) => {
			const data = JSON.parse(event.data)
			if (data.type === 'frame') screen.src = `data:image/jpeg;base64,${data.data}`
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') message.textContent = data.message
			else if (data.type === 'recipe') {
				// What an earlier recording picked stays unless picked again.
				recipe = {
					...data.recipe,
					steps: data.recipe.steps.length ? data.recipe.steps : recipe.steps || [],
					start: data.recipe.start || recipe.start || '',
					follow: data.recipe.follow.length ? data.recipe.follow : recipe.follow || [],
					links: data.recipe.links && data.recipe.links.selector ? data.recipe.links : recipe.links || {},
					next: data.recipe.next || recipe.next,
				}
				proposals = data.proposals
				message.textContent = ''
				renderPanel()
			}
		})
		socket.addEventListener('close', () => { message.textContent ||= 'The recorder’s browser closed.' })

		function point(event) {
			const box = screen.getBoundingClientRect()
			return { x: (event.clientX - box.left) * VIEWPORT.width / box.width, y: (event.clientY - box.top) * VIEWPORT.height / box.height }
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
				screen.focus()
				send({ type: 'mouse', event: kind, button: event.button === 2 ? 'right' : 'left', ...point(event) })
			})
		}
		screen.addEventListener('contextmenu', (event) => event.preventDefault())
		screen.addEventListener('wheel', (event) => { event.preventDefault(); send({ type: 'wheel', dx: event.deltaX, dy: event.deltaY }) }, { passive: false })
		screen.addEventListener('keydown', (event) => {
			if ((event.ctrlKey || event.metaKey) && event.key === 'v') return // the paste event sends the text
			event.preventDefault()
			const modifiers = ['Control', 'Alt', 'Meta', 'Shift'].filter((m) => event.getModifierState(m) && m !== event.key)
			const key = event.key.length === 1 && modifiers.every((m) => m === 'Shift') ? event.key : [...modifiers, event.key === ' ' ? 'Space' : event.key].join('+')
			send({ type: 'key', key })
		})
		screen.addEventListener('paste', (event) => {
			event.preventDefault()
			send({ type: 'text', text: event.clipboardData.getData('text') })
		})

		root.replaceChildren(el('div', { class: 'harvester-recorder' },
			el('h2', { text: 'Recorder' }),
			el('p', { text: 'Sign in and go to the listing as you would (passwords are not recorded: the job uses its account). Then pick the links to follow (if the files are a level deeper) and the file links: their patterns are proposed, the matches outlined.' }),
			el('div', { class: 'harvester-toolbar' },
				url,
				el('button', { text: 'Go', onclick: () => send({ type: 'goto', url: url.value }) }),
				el('button', { text: 'Back', onclick: () => send({ type: 'back' }) }),
				el('button', { text: 'Undo step', onclick: () => send({ type: 'undo' }) }),
				el('button', { text: 'Clear picks', onclick: () => { recipe = { steps: recipe.steps }; send({ type: 'clear' }) } }),
				el('button', { class: 'primary', text: 'Done', onclick: () => close(true) }),
				el('button', { text: 'Cancel', onclick: () => close(false) })),
			modes, modeHelp, message,
			el('div', { class: 'harvester-stage' }, screen, panel)))
		renderPanel()
		url.addEventListener('keydown', (event) => { if (event.key === 'Enter') send({ type: 'goto', url: url.value }) })
		url.focus()
	}

	function renderForm(root, job, prefill) {
		const value = job || prefill || BLANK
		const name = el('input', { value: value.name, required: '' })
		const target = el('input', { value: value.target })
		const schedule = el('input', { value: value.schedule })
		const tags = el('input', { value: value.tags.join(', ') })
		const enabled = el('input', { type: 'checkbox' })
		enabled.checked = value.enabled !== false
		const options = el('fieldset', {})
		const credentials = el('div', { class: 'harvester-credentials' })
		const error = el('p', { class: 'harvester-error' })
		const chosen = sources.find((s) => s.name === value.source) || sources[0]
		const optionInputs = {}
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
					el('button', { type: 'button', text: 'Record in the browser', onclick: () => openRecorder(root, page, textarea, applySuggestions) }))
			} else {
				optionInputs[key] = el('input', { value: current, type: typeof option.default === 'number' ? 'number' : 'text' })
				options.append(field(option.title, optionInputs[key], option.description))
			}
		}

		const form = el('form', {
			class: 'harvester-form',
			onsubmit: async (event) => {
				event.preventDefault()
				const body = {
					name: name.value,
					source: chosen.name,
					target: target.value,
					schedule: schedule.value,
					tags: tags.value.split(','),
					enabled: enabled.checked,
					options: Object.fromEntries(Object.entries(optionInputs).map(([key, input]) =>
						[key, typeof chosen.options[key].default === 'number' ? Number(input.value) : input.value])),
				}
				try {
					const saved = await api(job ? 'PUT' : 'POST', job ? `/jobs/${job.id}` : '/jobs', body)
					// A new job's account can be set once it exists: the form stays open for it.
					if (job) show(root)
					else renderForm(root, saved)
				} catch (e) {
					error.textContent = e.message
				}
			},
		},
		el('h2', { text: job ? `Edit ${job.name}` : 'New job' }),
		field('Name', name),
		field('Folder', target, 'In your files, e.g. /Magazines/iX.'),
		field('Schedule', schedule, 'Cron: minute hour day month weekday (UTC). 0 6 * * * is daily at 06:00.'),
		field('Tags', tags, 'Comma-separated; may use the link’s values, e.g. {year}. A tag whose value a file lacks is left out.'),
		field('Enabled', enabled),
		options,
		error,
		el('button', { class: 'primary', type: 'submit', text: 'Save' }),
		el('button', { type: 'button', text: 'Back to the jobs', onclick: () => show(root) }))
		page = el('div', {}, form, credentials)
		root.replaceChildren(page)
		renderCredentials(credentials, job, chosen)
	}

	async function show(root) {
		try {
			await renderJobs(root)
		} catch (e) {
			root.replaceChildren(el('p', { class: 'harvester-error', text: `Harvester: ${e.message}` }))
		}
	}

	document.head.append(el('style', {}, `
		#harvester { padding: 16px 24px; max-width: 1500px; }
		#harvester table { width: 100%; border-collapse: collapse; margin: 12px 0; }
		#harvester th, #harvester td { text-align: left; padding: 6px; border-bottom: 1px solid var(--color-border); }
		#harvester .harvester-field { display: flex; flex-direction: column; margin: 8px 0; max-width: 700px; }
		#harvester .harvester-field small { color: var(--color-text-maxcontrast); }
		#harvester fieldset { border: 1px solid var(--color-border); padding: 8px 12px; margin: 12px 0; }
		#harvester .harvester-error { color: var(--color-error); }
		#harvester .harvester-credentials { margin-top: 24px; }
		#harvester .harvester-card { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 12px 16px; margin: 12px 0; max-width: 760px; }
		#harvester .harvester-recipe, #harvester .harvester-pattern { font-family: monospace; width: 100%; }
		#harvester .harvester-toolbar { display: flex; gap: 4px; margin: 8px 0; }
		#harvester .harvester-url { flex: 1; }
		#harvester .harvester-stage { display: flex; gap: 12px; align-items: flex-start; }
		#harvester .harvester-screen { flex: 3; min-width: 0; aspect-ratio: 1280 / 800; border: 1px solid var(--color-border); background: var(--color-background-dark); cursor: crosshair; }
		#harvester .harvester-screen:focus { outline: 2px solid var(--color-primary-element); }
		#harvester .harvester-steps { flex: 1; min-width: 300px; max-height: 80vh; overflow: auto; overflow-wrap: anywhere; font-size: 90%; }
		#harvester .harvester-level { border-left: 3px solid var(--color-primary-element); padding-left: 8px; margin: 8px 0; }
		#harvester .harvester-value { background: var(--color-primary-element-light); border-radius: 4px; padding: 0 4px; margin-right: 4px; font-family: monospace; }
	`))

	document.addEventListener('DOMContentLoaded', async () => {
		const root = el('div', { id: 'harvester' })
		;(document.querySelector('#content') || document.body).append(root)
		;[sources, presets] = await Promise.all([api('GET', '/sources'), api('GET', '/presets')])
		show(root)
	})
})()
