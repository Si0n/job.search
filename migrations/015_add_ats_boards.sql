-- Employers' own ATS boards, behind one source.
--
-- Every other source here is a job board: it lists whoever pays to be listed,
-- and a posting's domain has to be recovered from its text afterwards. This one
-- inverts that -- the employers are named up front, so domain fit is settled
-- before a posting is fetched. That is the gap the scores keep showing:
-- `domain_fit` is the dimension the aggregators lose on, and no source here was
-- fintech-native.
--
-- Sixty-five boards across three ATS vendors, each verified to answer with live
-- postings. Companies were dropped where the slug resolved to someone else --
-- `wise` is a US field-sales firm rather than the money transfer one, `circle`
-- is the community platform, `lunar` is a US healthcare provider -- and where a
-- board answered with nothing (kraken, deel, spendesk, increase).
--
-- Priority 12 puts these above the aggregators: the body is the employer's own
-- posting, so it should win as the canonical description when the same job also
-- arrives from a board.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('ats', TRUE, 'http-json', 12, 'https://boards-api.greenhouse.io');

-- Three lists, tuned against one full run of all sixty-five boards (roughly
-- five thousand open roles).
--
-- `keywords` match anywhere in the posting. These are mostly Java and Go shops
-- and not one of them puts PHP in a title, but the ones that use it name it in
-- the requirements: nineteen postings across the whole run, and they are the
-- valuable ones -- Mollie is a PHP shop and Adyen writes e-commerce plugins.
--
-- `fallback_keywords` match the TITLE ONLY. Every engineering description at a
-- payments company says "backend" somewhere, so matching the body on them
-- admits all five thousand -- the category-dump failure that took remoteok and
-- weworkremotely offline. Kept deliberately narrow: "software engineer" alone
-- matched 865 of the 5000, "backend" matches 200.
--
-- `exclude_title_keywords` veto on the title and veto everything, the stack
-- match included. A support role that names PHP in its requirements is still a
-- support role, and this is what separates Mollie "Application Engineer II"
-- from Mollie "Technical Support Specialist". Manager and director are here for
-- the same reason profile.yaml scores people management down.
--
-- Net effect: 885 postings before the exclusions, 189 after. Adding a company
-- later is an edit here, not a code change.
UPDATE sources SET
  query = '{
  "greenhouse": {
    "adyen": "Adyen",
    "affirm": "Affirm",
    "alloy": "Alloy",
    "betterment": "Betterment",
    "bitpanda": "Bitpanda",
    "block": "Block",
    "blockchain": "Blockchain.com",
    "brex": "Brex",
    "checkr": "Checkr",
    "chime": "Chime",
    "coinbase": "Coinbase",
    "consensys": "Consensys",
    "ebury": "Ebury",
    "fireblocks": "Fireblocks",
    "form3": "Form3",
    "gemini": "Gemini",
    "gocardless": "GoCardless",
    "highnote": "Highnote",
    "lithic": "Lithic",
    "marqeta": "Marqeta",
    "melio": "Melio",
    "mercury": "Mercury",
    "monzo": "Monzo",
    "n26": "N26",
    "okx": "OKX",
    "payoneer": "Payoneer",
    "raisin": "Raisin",
    "ripple": "Ripple",
    "robinhood": "Robinhood",
    "sofi": "SoFi",
    "solarisbank": "Solaris",
    "stripe": "Stripe",
    "sumup": "SumUp",
    "thunes": "Thunes",
    "toast": "Toast",
    "truelayer": "TrueLayer"
  },
  "ashby": {
    "airwallex": "Airwallex",
    "column": "Column",
    "freetrade": "Freetrade",
    "ledger": "Ledger",
    "middesk": "Middesk",
    "modernTreasury": "Modern Treasury",
    "mollie": "Mollie",
    "oyster": "Oyster",
    "paddle": "Paddle",
    "persona": "Persona",
    "plaid": "Plaid",
    "pleo": "Pleo",
    "ramp": "Ramp",
    "sardine": "Sardine",
    "satispay": "Satispay",
    "sentilink": "SentiLink",
    "socure": "Socure",
    "swan": "Swan",
    "trustly": "Trustly",
    "unit": "Unit",
    "zilch": "Zilch"
  },
  "lever": {
    "anchorage": "Anchorage Digital",
    "binance": "Binance",
    "dlocal": "dLocal",
    "finix": "Finix",
    "nium": "Nium",
    "qonto": "Qonto",
    "wealthfront": "Wealthfront",
    "younited": "Younited"
  },
  "keywords": [
    "PHP",
    "Laravel",
    "Symfony"
  ],
  "fallback_keywords": [
    "backend",
    "back-end",
    "back end",
    "payments engineer",
    "api engineer",
    "integration engineer"
  ],
  "exclude_title_keywords": [
    "frontend",
    "front-end",
    "front end",
    "mobile",
    "ios",
    "android",
    "data",
    "machine learning",
    "security",
    "qa",
    "quality",
    "growth",
    "analytics",
    "embedded",
    "hardware",
    "firmware",
    "site reliability",
    "network",
    "solution",
    "sales",
    "forward deployed",
    "manager",
    "director",
    "support",
    "administrator",
    "supervisor",
    "recruiter",
    "counsel",
    "accountant",
    "analyst",
    "intern",
    "designer",
    "marketing",
    "legal",
    "compliance",
    "payroll",
    "operations"
  ]
}',
  selectors = '{
  "root": "",
  "skip_first": false,
  "fields": {
    "external_id": "external_id",
    "url": "url",
    "title": "title",
    "company": "company",
    "location": "location",
    "description": "description",
    "posted_at": "posted_at",
    "arrangement_hint": "arrangement_hint",
    "employment_hint": "employment_hint"
  }
}'
WHERE name = 'ats';
