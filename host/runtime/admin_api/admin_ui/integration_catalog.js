// Operator-facing catalog for network integrations that are built into the
// host rather than supplied by tool manifests. The compact info popover and
// the full Integration Guides entry render this same content.

export const MANAGED_INTEGRATIONS = {
  openai: {
    label: "OpenAI",
    summary: "Connect your OpenAI subscription and let your agent use Codex for tasks and cached web search.",
    protections: [
      "The linked OpenAI account is pinned. Authenticated traffic for another account is denied until you explicitly disconnect and log in again.",
      "Live browsing and remote tool servers are blocked. Codex can use only OpenAI's cached web search.",
    ],
    setupSteps: [
      { title: "Enable OpenAI", description: "In Internet Access and Tools, choose Enable on the OpenAI row, then expand it." },
      { title: "Start the Codex login", description: "In Account, choose Start Codex login. In the OpenAI browser sign-in, use the subscription you want this host to use and enter the displayed device code to complete sign-in." },
      { title: "Verify the linked account", description: "Return to TrustyClaw and wait for the row to show connected with the expected email or account id. That identity is now the operator-approved account anchor." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to Codex can go to OpenAI, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "OpenAI", text: "Everything the agent sends goes to OpenAI's services under the linked account." },
            { label: "Service providers", text: "OpenAI shares selected content onward with its trusted service providers for safety and data annotation, so data can leave OpenAI itself." },
            { label: "Web search", text: "Cached web search keeps the search query and surrounding context within OpenAI; no external site is contacted for the request." },
          ],
          links: [],
        },
        {
          title: "What OpenAI can do with it",
          description: "This guide assumes a personal ChatGPT/Codex OAuth subscription. One account setting, Improve the model for everyone, controls training use.",
          points: [
            { label: "Before connecting", text: "Turn off Improve the model for everyone in ChatGPT Settings > Data Controls. While it is on, OpenAI may use new conversations and Codex content to improve its models; once off, OpenAI says new conversations are not used for model training. The setting changes training use, not retention." },
            { label: "Either way", text: "Limited reviewers may access content for abuse or security investigations, support, or legal matters." },
          ],
          links: [
            { url: "https://help.openai.com/en/articles/7730893-chatgpt-data-usage-for-model-training", label: "OpenAI Data Controls instructions" },
            { url: "https://help.openai.com/en/articles/7039943", label: "OpenAI consumer data usage FAQ" },
            { url: "https://openai.com/policies/privacy-policy/", label: "OpenAI Privacy Policy" },
          ],
        },
        {
          title: "How long OpenAI retains it",
          description: "Codex chats and their content remain saved until you delete them.",
          points: [
            { label: "After deletion", text: "OpenAI schedules permanent deletion within 30 days unless data was de-identified, disassociated from your account, or must be kept for security or legal reasons." },
          ],
          links: [
            { url: "https://help.openai.com/en/articles/20001333-how-to-archive-and-delete-codex-chats-in-the-chatgpt-app", label: "OpenAI Codex retention and deletion" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Codex model access", description: "Runs Codex tasks through the models and usage limits available to the linked OpenAI subscription." },
      { name: "Cached web search", description: "Lets Codex search OpenAI's existing index or cache. TrustyClaw denies request forms that would let OpenAI fetch live external pages for the request." },
    ],
    controls: [
      "The proxy fails closed when the account pin or request body cannot be checked.",
    ],
    networkScope: [
      ["api.openai.com", "POST; pinned-account and external-URL request guards"],
      ["auth.openai.com", "GET and POST for the operator login flow"],
      ["chatgpt.com", "GET and POST; pinned-account and external-URL request guards"],
    ],
  },
  claude: {
    label: "Claude",
    summary: "Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.",
    protections: [
      "The linked Anthropic account and OAuth token are pinned. Credentials for another account are denied until you explicitly disconnect and log in again.",
      "Web search is off by default. When you enable it, the query and surrounding context reach Anthropic's server-side search, which may use search partners and retrieve source pages outside TrustyClaw's boundary. Server-side web fetch, code execution, and remote tool servers stay blocked at the proxy regardless; the agent's own web fetch runs on this host and can reach only TrustyClaw's allowed domains.",
    ],
    setupSteps: [
      { title: "Enable Claude", description: "In Internet Access and Tools, choose Enable on the Claude row, then expand it." },
      { title: "Start the Claude Code login", description: "In Account, choose Start Claude Code login. Follow the displayed Anthropic OAuth flow and paste the authorization result when prompted." },
      { title: "Verify the linked account", description: "Wait for the row to show connected with the expected Anthropic identity. TrustyClaw validates the token live before reporting the runtime active." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to Claude Code can go to Anthropic, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "Anthropic", text: "Everything the agent sends goes to Anthropic's services under the linked account, with service providers used to operate Claude." },
            { label: "Search partners (only if web search is enabled)", text: "With web search enabled, the query may go to Anthropic's search partners and Anthropic may retrieve source pages, outside TrustyClaw's network boundary. Anthropic does not name which third-party search providers it uses. With web search off (the default), nothing leaves for search." },
          ],
          links: [],
        },
        {
          title: "What Anthropic can do with it",
          description: "This guide assumes a personal Claude Free, Pro, or Max OAuth subscription used with Claude Code. One account setting, Help Improve Claude, controls training use.",
          points: [
            { label: "Before connecting", text: "Turn off Help Improve Claude in Claude Settings > Privacy. While it is on, Anthropic may use new personal chats and Claude Code sessions to improve Claude; once off, past and new chats or coding sessions are not used for future model training, though training already underway is unaffected." },
            { label: "Regardless", text: "Safety-flagged conversations may still be analyzed for policy enforcement and to improve Anthropic's safeguards." },
          ],
          links: [
            { url: "https://privacy.claude.com/en/articles/12109829-how-do-i-change-my-model-improvement-privacy-settings", label: "Anthropic model improvement setting instructions" },
            { url: "https://privacy.claude.com/en/articles/10023580-is-my-data-used-for-model-training", label: "Anthropic consumer training policy" },
          ],
        },
        {
          title: "How long Anthropic retains it",
          description: "Personal conversations remain until you delete them; Anthropic says deletion removes them from history immediately and from backend storage within 30 days.",
          points: [
            { label: "Covered Models", text: "Anthropic designates its most capable models, including Fable 5, as Covered Models with an extra safety measure: prompts and outputs are kept for 30 days on every plan, even with model improvement off. After 30 days they are deleted automatically unless a safety investigation or legal obligation requires longer." },
            { label: "Safety flags", text: "Anthropic may retain flagged inputs and outputs for up to 2 years and trust-and-safety classification scores for up to 7 years." },
            { label: "Feedback and de-identified data", text: "Feedback may be kept for 5 years; anonymized or de-identified data may be kept longer." },
          ],
          links: [
            { url: "https://privacy.anthropic.com/en/articles/10023548-how-long-do-you-store-my-data", label: "Anthropic consumer retention policy" },
            { url: "https://support.claude.com/en/articles/15425695-covered-models", label: "Anthropic Covered Models retention" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Claude Code model access", description: "Runs Claude Code tasks through the models and usage limits available to the linked Anthropic subscription." },
      {
        name: "Web search (optional, off by default)",
        description: "Off unless you enable it for the Claude integration. When on, Anthropic runs the search server-side: the query and surrounding context leave to Anthropic and its search partners — Anthropic does not name which third-party search providers it uses.",
        linkUrl: "https://support.claude.com/en/articles/10684626-enable-and-use-web-search",
        linkLabel: "Anthropic web search documentation",
      },
    ],
    controls: [
      "A token rotation is re-attested to Anthropic and must still match the operator-approved account.",
    ],
    networkScope: [
      ["api.anthropic.com", "GET and POST; pinned-account, OAuth-token, and server-side web-tool guards"],
      ["platform.claude.com", "GET and POST only for the Claude OAuth endpoints"],
    ],
  },
  github: {
    label: "GitHub",
    summary: "Connect GitHub and let your agent read repositories and write only to the repositories you choose.",
    protections: [
      "Reads can reach any public repository and private repositories visible to the credential; writes work only for the repositories you configure.",
      "Repository administration, GraphQL, Git LFS uploads, and other write paths that could reach beyond the configured repositories stay denied.",
      "Keep approval for `.github` pushes enabled. Workflow changes can make GitHub Actions run arbitrary code with network access and repository credentials.",
    ],
    setupSteps: [
      { title: "Choose a credential mode", description: "Use a fine-grained personal access token for the simplest personal setup. Use a GitHub App when you want repository installation scope and short-lived minted tokens." },
      { title: "Create a fine-grained token", description: "In GitHub Settings > Developer settings > Personal access tokens > Fine-grained tokens, choose Generate new token. Select the resource owner and only the repositories this host should reach. Grant Contents read/write for Git pushes, Metadata read, and only the additional repository permissions required by the REST actions you intend to use.", linkUrl: "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens", linkLabel: "View GitHub's fine-grained token guide" },
      { title: "Or create and install a GitHub App", description: "In GitHub Settings > Developer settings > GitHub Apps, create an app with only the repository permissions your workflow needs. Install it on the selected repositories, note the App ID and installation ID, then generate and download a private key. TrustyClaw uses those values to mint short-lived installation tokens.", linkUrl: "https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app", linkLabel: "View GitHub's app registration guide" },
      { title: "Store the credential", description: "Enable GitHub, expand the row, select the credential type, enter its values, and choose Set credential. Stored secret values are never read back into the UI." },
      { title: "Add write repositories", description: "Under Write repositories, add each owner/repository that may receive a push or mutating REST API call. Repositories not listed remain read-only." },
      { title: "Keep .github push approval enabled", description: "TrustyClaw enables Require approval for .github pushes when GitHub is first turned on. Keep it enabled so workflow and other .github path changes are held for an operator decision; GitHub Actions workflows can execute arbitrary code with network access and repository credentials." },
    ],
    capabilities: [
      { name: "Git and REST reads", description: "Clone, fetch, inspect releases and raw files, and use read-only GitHub REST endpoints wherever the credential has access." },
      { name: "Scoped Git and REST writes", description: "Push and call mutating repository REST endpoints only for configured write repositories." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Any data on this host can be written to a repository on the write list, so assume GitHub can receive anything the agent can read here. Reads send only repository paths and query parameters, but GitHub receives and logs that request text with standard metadata whether or not the requested repository exists, so anything the agent puts in a path or query is itself disclosed to GitHub.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "Write repositories", text: "Apart from public repositories and GitHub Actions (below), data can go only to the repositories on your write list; in a private repository it is visible only to that repository's collaborators." },
            { label: "Public repositories", text: "Everything pushed to a public write repository is exposed to the entire internet." },
            { label: "GitHub Actions", text: "A push changing a .github path can start workflow runs, which execute code with network access and can send repository data anywhere. TrustyClaw holds .github pushes for your approval by default." },
          ],
          links: [],
        },
        {
          title: "What GitHub can do with it",
          description: "GitHub processes pushed content and account, repository, and usage data under its Privacy Statement; who else can see pushed data is set by the repository's visibility and organization settings.",
          links: [
            { label: "GitHub General Privacy Statement", url: "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement" },
            { label: "GitHub App permissions", url: "https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app" },
          ],
        },
        {
          title: "How long GitHub retains it",
          description: "Repository content remains until it is changed or deleted, and public content may be copied or forked by anyone while it is visible. GitHub keeps account data while the account is active and as needed for contracts, legal obligations, disputes, or enforcement.",
          links: [
            { label: "GitHub General Privacy Statement", url: "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement" },
          ],
        },
      ],
    },
    controls: [
      "Disabling GitHub clears the write-repository list; the independently stored credential can remain staged or be cleared separately.",
    ],
    networkScope: [
      ["github.com", "GET, HEAD, and fetch for any visible repository; push only to write repositories; LFS uploads denied"],
      ["api.github.com", "GET and HEAD broadly; repository REST writes only for write repositories; GraphQL denied; administration denied"],
      ["uploads.github.com", "Release-asset uploads only for write repositories"],
      ["codeload.github.com", "GET and HEAD for any visible repository archive"],
      ["raw.githubusercontent.com", "GET and HEAD for any visible repository path"],
      ["objects.githubusercontent.com", "GET and HEAD for signed download URLs only"],
      ["github-cloud.githubusercontent.com", "GET and HEAD for signed download URLs only"],
      ["release-assets.githubusercontent.com", "GET and HEAD for signed release-asset URLs only"],
    ],
  },
  python_packages: {
    label: "Python packages",
    summary: "Lets your agent discover and install public Python packages from PyPI.",
    protections: [
      "Access is read-only and limited to the public PyPI index, package metadata, and distribution download paths.",
      "Package publishing and arbitrary requests to PyPI or the download host remain denied.",
    ],
    setupSteps: [
      { title: "Enable Python packages", description: "Choose Enable in Internet Access and Tools. pip and compatible package clients can then resolve and download public distributions." },
    ],
    capabilities: [
      { name: "Package discovery", description: "Reads the PyPI simple index and package JSON metadata." },
      { name: "Distribution downloads", description: "Downloads wheels and source archives from PyPI's package file host." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Only package names and versions, the files requested, and standard web request metadata (source IP, request time, client User-Agent). Nothing else on this host is sent.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "PyPI", text: "Requests go to PyPI, run by the Python Software Foundation with infrastructure providers including AWS and Fastly." },
            { label: "Public download dataset", text: "Each package download is recorded in a public statistics dataset: the package, file, client tool, and an approximate location derived from the source IP. Plain metadata lookups are not included." },
            { label: "Nonexistent packages", text: "A request for a package name that does not exist still reaches PyPI and its request logs like any other request, so requested-name text is itself data sent to PyPI. It does not enter the public dataset." },
          ],
          links: [
            { label: "PyPI public download dataset", url: "https://docs.pypi.org/api/bigquery/" },
          ],
        },
        {
          title: "What PyPI can do with it",
          description: "PyPI uses request logs to operate and secure the index; it does not sell them or use them for advertising. PyPI says its retained download logs contain no IP addresses.",
          links: [
            { label: "PyPI Privacy Notice", url: "https://policies.python.org/pypi.org/Privacy-Notice/" },
          ],
        },
        {
          title: "How long PyPI retains it",
          description: "PyPI does not publish a fixed retention period for ordinary request logs. Entries in the public download dataset remain available indefinitely, but they identify only the package and download context, not you.",
          links: [
            { label: "PyPI Privacy Notice", url: "https://policies.python.org/pypi.org/Privacy-Notice/" },
          ],
        },
      ],
    },
    networkScope: [
      ["pypi.org", "GET and HEAD only under /simple and /pypi/<package>/json"],
      ["files.pythonhosted.org", "GET and HEAD only under /packages"],
    ],
  },
  npm_packages: {
    label: "NPM Packages",
    summary: "Lets your agent discover and install public JavaScript packages and download Node.js releases.",
    protections: [
      "Registry and Node.js distribution access is read-only; npm publishing and arbitrary Node.js website paths remain denied.",
      "Only public registry data and release files are available through this integration.",
    ],
    setupSteps: [
      { title: "Enable NPM Packages", description: "Choose Enable in Internet Access and Tools. npm and compatible clients can then resolve and download public packages and Node.js distributions." },
    ],
    capabilities: [
      { name: "npm registry reads", description: "Reads public package metadata and tarballs through registry.npmjs.org." },
      { name: "Node.js downloads", description: "Downloads published Node.js distributions from the official /dist path." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Only package names and versions, the files requested, and standard web request metadata (source IP, request time, client User-Agent). Nothing else on this host is sent.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "npm registry", text: "Package requests go to npm's registry, operated by GitHub, which stores registry-use information in the United States." },
            { label: "nodejs.org", text: "Node.js downloads go to the OpenJS Foundation's website infrastructure." },
            { label: "Public counts", text: "Only aggregate per-package download counts are published; they contain nothing about you or this host." },
            { label: "Nonexistent packages", text: "A request for a package name that does not exist still reaches the registry and its request logs like any other request, so requested-name text is itself data sent to npm. Nothing about it is published." },
          ],
          links: [],
        },
        {
          title: "What npm and OpenJS can do with it",
          description: "npm uses registry request logs to operate and secure the registry. OpenJS processes nodejs.org download request metadata the same way under its website Privacy Policy.",
          links: [
            { label: "npm Privacy Policy", url: "https://docs.npmjs.com/policies/privacy/" },
            { label: "OpenJS Foundation Privacy Policy", url: "https://openjsf.org/privacy" },
          ],
        },
        {
          title: "How long npm and OpenJS retain it",
          description: "Neither policy states one fixed retention period for ordinary registry and download request logs. Aggregate download counts remain public, but they contain nothing about you or this host.",
          links: [
            { label: "npm public-registry terms", url: "https://docs.npmjs.com/policies/open-source-terms/" },
            { label: "OpenJS Foundation Privacy Policy", url: "https://openjsf.org/privacy" },
          ],
        },
      ],
    },
    networkScope: [
      ["registry.npmjs.org", "GET and HEAD only"],
      ["nodejs.org", "GET and HEAD only under /dist"],
    ],
  },
};

export const CUSTOM_DOMAIN_GUIDE = {
  id: "custom_domain",
  label: "Custom Domain Access",
  summary: "Creates an explicit network rule for a domain that is not covered by a managed integration or bundled tool.",
  protections: [
    "Every request must match the configured domain, method, and any path guards. Anything outside the rule is denied and recorded in the network audit log.",
    "Managed-integration domains are reserved, so a custom rule cannot bypass their account, repository, or request-body protections.",
  ],
  setupSteps: [
    { title: "Identify the narrow boundary", description: "Decide the smallest exact domain, method set, and path surface the workflow needs. Prefer an exact API host over a wildcard." },
    { title: "Add the rule", description: "Expand Custom Domain Access, enter the domain, comma-separated HTTP methods, and optional path regexes one per line, then choose Add domain rule." },
    { title: "Verify in the audit log", description: "Run the intended request and inspect Network audit log. A denial gives a dedicated reason; widen only the specific boundary the real request proves necessary." },
  ],
  capabilities: [
    { name: "Custom HTTPS access", description: "Allows agent traffic to operator-selected third-party API or download hosts within the rule." },
  ],
  dataSummary: {
    items: [
      {
        title: "What leaves this host",
        description: "The configured service receives the complete HTTPS request: hostname, path, query parameters, method, headers, cookies or authorization values, body, and source network metadata. Any host data the agent places in a request can go to that service.",
        links: [],
      },
      {
        title: "Where it can go",
        description: "Directly to the configured domain, and from there wherever that service's own terms allow. TrustyClaw applies no redaction and holds no contract limiting onward sharing.",
        links: [],
      },
      {
        title: "What the third party can do with it",
        description: "TrustyClaw adds only the configured network boundary. It provides no provider contract, field redaction, OAuth isolation, or data-use promise; the service's own current terms control what it does with the data.",
        links: [],
      },
      {
        title: "How long the third party retains it",
        description: "TrustyClaw does not know the configured service's retention or deletion practices. Check that service's current policy before sending personal, confidential, regulated, or credential-bearing data.",
        links: [],
      },
    ],
  },
  controls: [
    "Rules validate structurally and publish atomically; an invalid replacement leaves the active policy unchanged.",
  ],
  networkScope: [],
};

export function integrationInfo(name) {
  return MANAGED_INTEGRATIONS[name];
}
