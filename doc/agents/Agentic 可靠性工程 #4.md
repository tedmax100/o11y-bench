---
title: 'Agentic 可靠性工程 #4'

---

# Chapter 7. Agent-Driven Incident Response Pipeline
A Note for Early Release Readers
With Early Release ebooks, you get books in their earliest form—the author’s raw and unedited content as they write—so you can take advantage of these technologies long before the official release of these titles.

This will be the 5th chapter of the final book. Please note that the GitHub repo will be made active later on.

If you’d like to be actively involved in reviewing and commenting on this draft, please reach out to the editor at jleonard@oreilly.com.

The same incident, run by five different organisations, will produce five different outcomes. Same alert, same underlying cause, same blast radius. One organisation pages a senior on-call who assembles context from six tools in twelve minutes and ships a rollback. Another organisation has an agent that throttles non-critical paths in 23 seconds and pages nobody. The technology is not what separates the two. The architecture is. Section 5.10 develops this picture in full, the same incident played through all five maturity levels in deliberate detail. The rest of the chapter is the apparatus that makes the comparison meaningful.

Incident response is the first workflow this book applies the four-plane architecture to, and the choice is deliberate. Incidents are high-frequency, high-cost, time-critical, and well-understood. Every reader has been on call. Every reader has assembled context under time pressure. Every reader has shipped a fix while uncertain whether it would work. The workflow is one of the few that nearly every SRE has lived through identically, which makes it the cleanest test for whether Agentic Reliability Engineering earns its keep. If ARE cannot operate safely during an incident, when blast radius is growing and decisions matter most, then nothing else in the book is worth implementing.

This chapter is also where the recurring cast becomes visible. Part I named several agents in passing. Part II is where they earn their place by participating in the same scenes. The Observability Agent maintains decision-grade signals continuously, not just when something fires. The Topology Agent owns the graph of dependencies and ownership that Chapter 2 named the canonical resilience artefact. The Release Agent supplies change context. The Business Impact Agent translates “service X is degraded” into “users Y are affected with consequence Z.” The Learning Agent, introduced fully in section 5.8, closes the loop by turning every incident into structured updates to the system’s models of itself. These are not separate products. They are roles inside the four-plane architecture, each owning a slice of the Signal or Reasoning Plane, each producing structured artefacts the other agents can consume. This chapter is where the reader meets them in action.

The structural property worth flagging upfront is that every section in this chapter maps to a beat of the DRAL loop from Chapter 3.1. Sections 5.1 through 5.4 develop the Detect beat in progressively higher resolution. Section 5.5 develops the Reason beat. Section 5.6 develops the Act beat. Section 5.8 develops the Learn beat. Section 5.7 covers human-in-the-loop as it specifically applies to incident response, referencing back to Chapter 4.7 as the canonical treatment. Sections 5.9 and 5.10 measure and compare. Read in order, the chapter is one continuous loop applied to a single workflow. Read out of order, each section still stands, because the architecture is the same.

We will follow a single canonical scenario throughout this chapter. A tier-1 latency degradation on the checkout journey of a payment-processing service. The same scenario that anchored the worked example in Chapter 3.1 and the reference architecture walk-through in Chapter 4.8. The reader has already seen the agent throttle non-critical paths once. This chapter develops every other beat of the loop around that scenario, then in 5.10 plays the same incident at five maturity levels to show how the architecture changes the outcome.

A note about scope before we begin. This chapter is the canonical treatment of incident response, but it is not exhaustive. Specific failure classes (data-plane outages, security incidents, cascading multi-region failures) have their own variations of the patterns developed here, and Chapters 7 and 8 will pick up some of them in the context of chaos engineering and pre-incident work respectively. What this chapter develops is the general shape of agent-driven incident response: the architecture, the agents, the artefacts they produce, the SLOs that measure them, and the maturity progression that determines what a team can safely operate. Once the shape is internalised, the variations are straightforward extensions, not new frameworks.

5.1 From Alerts to Situational Awareness
Alerts are not a model of reality. They are fragments of it.

Traditional alerting stacks are optimised for human attention. They emit notifications when a threshold is crossed and assume a human will infer context, correlate signals, and decide what matters. The assumption was reasonable when systems were smaller and slower. It does not hold for distributed systems whose state changes faster than a human can read a dashboard. The Detect beat of DRAL needs a different artefact, and this section names it. Situational awareness, in an agentic incident response pipeline, is not a state of mind. It is a deliverable. The system produces it. The human (or the agent above the Detect beat) consumes it.

Consider what situational awareness has to contain to be useful. The signal that fired, with its full contract attached. The historical baseline against which the deviation is measured. The topology context: which service is degraded, which dependencies it has, which downstream consumers will be affected. The change context: what shipped in the last hour, what configuration was modified, what was rolled out to which cohorts. The ownership metadata: who owns the service, who owns the dependency, which on-call rotation is implicated. The confidence band on each of these inputs, because some of them will be fresh and some stale. None of this is novel. Every senior on-call engineer assembles this picture by hand during the first two minutes of every incident. What changes in an agentic architecture is that the assembly is not a human’s job. The Signal Plane already maintains all of these artefacts continuously, and the Observability Agent and Topology Agent compose them into the situational-awareness object the moment the signal crosses its contract’s significance threshold.

To make this concrete, return to the canonical scenario. At t=0, p99 latency on the `/api/v2/cart/submit` endpoint begins to drift upward. The Observability Agent has been emitting structured latency signals continuously, against a signal contract that declares the steady-state band, the freshness guarantee, and the decisions the signal is permitted to support. At t=15 seconds, the deviation exceeds significance. The Observability Agent emits the signal with full context attached. The Topology Agent attaches the dependency graph: payment-gateway is upstream and reports a degraded state three minutes old; inventory-api is upstream and was deployed four minutes ago; the checkout journey touches four downstream consumers whose traffic patterns are now visible. The Release Agent (cameoing here for the first time, full treatment in Chapter 6) attaches recent change events: the inventory-api deploy, a configuration push that landed yesterday but only became hot this morning, a feature-flag toggle on a different service. All of this lands in the situational-awareness object at t=18 seconds. A human reviewing this object knows everything they would have spent ten minutes assembling. An agent reading the object can begin reasoning immediately.

The architectural shift this names is small in description and large in consequence. The work that used to define the first minutes of every incident, the assembly of context from multiple tools, is no longer in the incident’s critical path. It moves earlier, into the continuous operation of the Signal Plane, and the situational-awareness object is the artefact that hands it off. The human is not staring at six dashboards at 03:00. The human is reading one structured object that already knows what the dashboards would have said and what they collectively imply. This is what we mean when we say agentic reliability moves the human above the loop. The human’s question stops being “what is happening” and becomes “given this picture, what should be done.”

Note
Situational awareness in agentic systems is not “more dashboards.” It is the elimination of the assembly step. Dashboards still exist for the cases where humans need to interrogate the underlying data directly. The default flow does not pass through them.

The other property worth naming is that situational awareness is versioned. The object emitted at t=18 is not the same object emitted at t=60. As the incident unfolds, signals arrive, hypotheses sharpen, contradictions resolve. The situational-awareness object is updated continuously, each version carrying a timestamp and a delta from the previous version. This matters for two reasons. First, downstream agents can reason about change in the situation, not just current state, which is what allows the Reason beat in section 5.5 to evaluate whether the situation is stabilising or worsening. Second, the audit trail that section 5.6 will require depends on being able to reconstruct exactly what the system believed at each moment. The versioned object provides that reconstruction artefact for free.

This is also where the Topology Agent first earns its keep across an incident. The dependency graph it maintains is not a wiki page. It is a live data structure validated against observed telemetry continuously, with ownership metadata and criticality tier embedded at every node. When the situational-awareness object asks “who is downstream of this service” or “who is on call for this dependency,” the answer is computed in milliseconds against a graph that was correct one minute ago. Chapter 2.4 established why this matters. This chapter is where the why becomes operational.

A practical question this section invites is what the architectural investment to build situational awareness actually costs. The answer varies by where a team is starting. A team that already has structured signal emission (Chapter 2’s decision-grade telemetry), an owned topology graph, and a release event stream is two-thirds of the way there; the remaining work is the Observability Agent and Topology Agent that compose these into the structured object. A team starting from raw metrics, ad-hoc deploys, and a wiki-page topology will spend a quarter or two on the substrate before agentic incident response is even feasible. This is one of the places where the maturity model from Chapter 4.9 is operationally honest. A team at L1 cannot build agentic situational awareness by sheer ambition; the substrate must be present first, and the substrate is the slow part. Once it is present, the agents that compose it are weeks of work, not quarters.

5.2 Agentic Incident Detection and Confidence Scoring
Situational awareness establishes what the system believes is happening. Detection is the next step: deciding that what is happening rises to the level of an incident, and beginning the work of remediation. In traditional operations, detection is a threshold-crossing event. The metric exceeds the line, the alert fires, the incident exists. In agentic incident response, detection is something subtler. It is a confidence-bearing assertion that the situational-awareness object describes a real degradation requiring response, paired with a structured estimate of how much the system trusts that assertion.

Confidence is the first-class output of detection in this architecture, and the conceptual move worth slowing down on is that the agent’s detection is not a verdict. It is a calibrated estimate. The Reasoning Plane attaches a confidence score to every hypothesis it generates: this is likely a dependency-saturation incident, confidence 0.82; this is likely a recent-change-induced regression, confidence 0.31; this is likely a traffic shift, confidence 0.14. The scores are computed from the inputs the situational-awareness object carries: the strength of the signal contract’s significance, the consistency across multiple signals, the historical success rate of similar reasoning patterns, the differentiation between competing hypotheses. The number is not a vibe. It is an output of the reasoning process, calibrated against past outcomes by the Learning Agent (section 5.8), measurable by the Calibration Error SLO from section 3.6.

The reason this matters in practice is that confidence is what the Governance Plane downstream evaluates to decide what level of autonomy is appropriate for this specific moment. A 0.82 confidence detection of dependency saturation, combined with a confidence-driven policy that allows autonomous mitigation above 0.80 for this service tier, results in the action executing without human approval. A 0.74 confidence detection routes the same proposal to a human. A 0.45 confidence detection produces no action at all; the agent escalates with the assessment but executes nothing. The same architecture handles all three cases through the same mechanism. The only thing that changes is the number, and the policy that interprets the number.

A worked example helps. The canonical scenario at t=22 seconds: the Observability Agent’s latency signal has been live for seven seconds. The Topology Agent reports payment-gateway in a known degraded state. The Release Agent reports the inventory-api deploy four minutes ago. The Reasoning Plane generates three hypotheses against the situational-awareness object:

  hypothesis_1:
	cause: "upstream_dependency_saturation"
	target: "payment-gateway"
	evidence:
  	- "payment-gateway reports degraded state for 3m"
  	- "checkout latency rose 8s after payment-gateway alert"
  	- "historical pattern: payment-gateway saturation produces this signature"
	confidence: 0.82
  hypothesis_2:
	cause: "recent_change_regression"
	target: "inventory-api"
	evidence:
  	- "inventory-api deploy 4m ago"
  	- "checkout traverses inventory-api on slow path"
  	- "no other deploys in past hour"
	confidence: 0.31
  hypothesis_3:
	cause: "traffic_pattern_shift"
	target: "checkout-api"
	evidence:
  	- "traffic up 8% versus same-time-yesterday"
  	- "no capacity alarm yet"
	confidence: 0.14
The Reasoning Plane does not collapse these into a verdict. It surfaces all three with their confidence scores and their evidence chains attached. The Governance Plane reads the leading hypothesis (0.82) against the policy for tier-1 services under dependency-degradation conditions. The policy allows autonomous mitigation above 0.80 confidence on this class. The action proceeds. The audit trail records all three hypotheses, the confidence scores, and the policy decision. Later, when the Learning Agent reviews the incident, the leading hypothesis can be validated against what actually turned out to be wrong, and the confidence calibration for this failure class is refined for next time.

The discipline worth naming is that calibration matters more than confidence in the long run. A 0.82 confidence assertion is useful only if 82% of similar assertions turn out to be correct. The Calibration Error SLO from section 3.6 is what measures this. A system whose stated 0.9 confidences are wrong 30% of the time has a CE problem regardless of how sophisticated its reasoning looks. A system whose stated 0.9 confidences are wrong 10% of the time has earned the right to operate autonomously above that threshold. The architecture surfaces both cases identically. The Calibration Error SLO is what distinguishes them. Chapter 9.4 will develop the formal calibration model in detail. For incident response, what matters is that the confidence number is computed honestly, surfaced to Governance, and measured against outcomes by Learning. Confidence that is not calibrated is not confidence. It is a guess presented as a verdict.

Warning
A detection without a confidence score is a guess presented as a verdict. The temptation to surface yes/no detections without their associated uncertainty is constant, especially when humans are downstream and used to alert-style binaries. Resist it. The number is the load-bearing part of the architecture. Without it, the Governance Plane has nothing to evaluate, and autonomy collapses back into either always-act or always-escalate.

One related property worth flagging is negative confidence. Sometimes the agent’s most useful output is a low-confidence detection that explicitly downgrades a noisy signal. A latency anomaly that fires during a known maintenance window, or during a load test, or in a region whose traffic pattern is currently atypical, should produce a lower confidence detection, not a suppressed one. The Governance Plane needs to see the assessment; it just needs to see it accompanied by the reason the agent’s confidence is reduced. This is the architectural alternative to alert suppression. Suppression hides information. Confidence reduction surfaces it with appropriate weight, which is what the downstream policy needs to make the right decision.

The architectural property worth naming as we move from detection toward action is that confidence is not a single number per incident. It is a structure that evolves over time. The initial confidence at t=22s in the canonical scenario is 0.82 on the leading hypothesis. By t=60s, with more evidence accumulated, the same hypothesis might be at 0.91. By t=120s, if the action taken at t=30s has not produced the expected recovery, the confidence might have dropped to 0.55 and the alternative hypothesis (the inventory-api change) might have risen from 0.31 to 0.68. The architecture tracks all of this. The Reasoning Plane updates its hypothesis ranking continuously. The Governance Plane re-evaluates the autonomy decision against the current state, not the state at the moment the initial proposal was made. This is what allows the architecture to recover gracefully from a wrong initial assessment: the confidence updates, the policy responds, the next action is appropriate to the current evidence rather than the original guess. Chapter 9 develops the formal model of how confidence is computed and updated. Chapter 11 develops how the Governance Plane responds to confidence changes through dynamic policy. For incident response, what matters here is that confidence is dynamic, the architecture treats it as dynamic, and the audit trail captures every confidence revision alongside the action it influenced.

5.3 Timeline Construction and Change Awareness
Detection establishes what is happening. The next question every incident commander asks, and every agentic system has to answer, is what changed. In modern distributed systems, many things change continuously: code releases, configuration pushes, feature-flag toggles, traffic-routing changes, dependency upgrades, infrastructure scaling. Without disciplined change awareness, incident diagnosis becomes guesswork. The agent that assembles a useful timeline does it from structured change events, correlated against the incident’s signal trajectory, with causal likelihood scored explicitly. This section is the Detect beat at its finest resolution.

The architectural prerequisite for any of this is that change is captured as data. Each deployment, each config push, each feature-flag toggle emits a structured event into the Signal Plane: who made the change, what it changed, when it shipped, which cohorts it affected, whether it is reversible, who owns it. The Release Agent is the role inside the Signal Plane that owns this stream. Chapter 6 will develop the Release Agent fully (it is the second proving ground of the book). For incident response, the Release Agent’s job is narrower: provide a queryable, time-ordered stream of change events that the Reasoning Plane can correlate against the incident’s signal trajectory. If this stream does not exist, agentic incident response degrades immediately. The agent cannot reason about causality from changes it cannot see, and the team falls back to humans reconstructing the change history from memory and chat logs, which is exactly the assembly step the architecture was designed to eliminate.

Timeline construction itself is a causal reasoning task, not a log search. The objective is not to list events chronologically. It is to determine which changes could plausibly have produced the observed signal trajectory, and which cannot. The agent scores each change event for causal likelihood against the leading hypothesis. Three factors dominate the scoring: temporal proximity (how close in time the change preceded the signal degradation), topological proximity (whether the change touched a service in the dependency path between the failing service and its degraded upstream), and historical alignment (whether changes of this type have produced similar signatures in the past). Each of these is computable from the artefacts the Signal Plane already maintains. The output is a ranked list of plausible change-side causes, attached to the situational-awareness object, available to the Reasoning Plane when it generates candidate actions in section 5.5.

EXAMPLE
The canonical scenario, with the timeline assembled at t=25s. The agent’s timeline view, simplified:

  incident: INC-2026-05-13-CHECKOUT-LATENCY
  detection_time: t=15s
  leading_hypothesis: dependency_saturation:payment-gateway
  recent_changes (last 60min, ranked by causal likelihood):
	- event_id: rel-payment-gateway-12847
  	type: deploy
  	target: payment-gateway
      timestamp: t-23m
      causal_likelihood: 0.74
      rationale: "in dependency path; touched code that handles the
                  request class showing degradation; historical similarity
                  to two prior payment-gateway latency incidents"
	- event_id: rel-inventory-api-44219
  	type: deploy
  	target: inventory-api
      timestamp: t-4m
      causal_likelihood: 0.28
      rationale: "in dependency path but on slow path only; small change
                  surface; no historical precedent for this signature"
	- event_id: cfg-traffic-shaper-9921
  	type: config_push
  	target: traffic-shaper
      timestamp: t-12m
      causal_likelihood: 0.09
      rationale: "in topology but not in this request's path; change
                  affected unrelated traffic class"
	- event_id: ff-toggle-recommendations-2241
  	type: feature_flag
  	target: recommendations-api
      timestamp: t-38m
      causal_likelihood: 0.03
      rationale: "outside dependency path for this incident"
The agent’s leading suspect is not the most recent change (the inventory-api deploy 4 minutes ago). It is the payment-gateway deploy from 23 minutes earlier. The agent knows this because temporal proximity is one input among several, not the dominant one. Historical alignment and topological position both favour the older change. A human glancing at the deploy log would have anchored on the most recent change. The agent does not.

The reason this matters is that wrong causal inference produces wrong action. A human who anchors on the inventory-api deploy because it is the most recent change will reach for rollback of that deploy. If the actual cause is the payment-gateway change, the rollback wastes time, may introduce its own degradation, and leaves the actual cause running. The agent, working from structured causal-likelihood scoring rather than recency bias, reaches for actions appropriate to the leading hypothesis (mitigating the payment-gateway saturation) while leaving the inventory-api deploy in place. This is one of the places where the architecture’s discipline produces visibly different outcomes from the human-led version of the same workflow, and the difference is not that the agent is smarter. The agent is doing the same causal reasoning the senior incident commander would do, except it is doing it in milliseconds against structured data, and the audit trail records the reasoning explicitly.

Timelines also need to be revisable. As the incident unfolds, new evidence arrives. A signal that was attributed to dependency saturation may turn out to be a symptom of something else. The Reasoning Plane updates the timeline as evidence accumulates, and the causal-likelihood scores shift. This is what prevents the system from anchoring on an early hypothesis past the point where the evidence supports it. Section 3.8 named this failure mode (silent learning corruption when outcomes reinforce wrong attributions). Timeline revisability is the architectural protection against it during the incident itself. The Learning Agent (section 5.8) handles the post-incident version of the same discipline, where the entire incident’s causal reconstruction can be revised when later evidence proves an early attribution wrong.

The other property worth naming is that timelines surface absence of change with the same weight as presence. When no change preceded the degradation, that itself is high-value information. It rules out the entire class of change-induced causes and refocuses the Reasoning Plane on environmental, traffic, or dependency-side hypotheses. A human under pressure tends to invent a change to explain a degradation, because the alternative (something just broke on its own) feels unsatisfying. The agent has no such bias. If the change stream is empty during the relevant window, the agent’s leading hypothesis will reflect that, and the action it proposes will be different.

5.4 Impact Analysis and Blast Radius Estimation
Detection tells the system that something is happening. Causal timeline tells it why. The next question, before any action is proposed, is how broadly is this spreading and who is affected. This is the work of the Business Impact Agent, making its first appearance in this chapter, and it is the bridge between technical degradation and the business-level decision the Governance Plane will eventually have to make.

The Business Impact Agent’s job is to translate “service X is degraded” into “users Y are affected with consequence Z.” That translation is not optional. Without it, the Reasoning Plane is reasoning about percentages of requests, and the Governance Plane is evaluating actions against policy without knowing what is actually at stake. With it, the same proposal carries an explicit estimate: this degradation is currently affecting 12% of checkout traffic, weighted toward EU customers in the evening peak, with an estimated revenue impact of £18,000 per hour at the current rate of spread, and the affected cohort includes three enterprise accounts whose SLAs trigger penalty clauses at 30 minutes of degradation. The number is not precise. It is calibrated. It is enough for the policy to evaluate whether the proposed mitigation’s cost is justified by the impact it is preventing.

The conceptual distinction worth drawing carefully is between blast radius and severity. Severity asks “how bad is the degradation”; it is a property of the technical state of the system. Blast radius asks “how broadly is this affecting users and downstream systems”; it is a property of who and what is exposed to the degradation. A tier-1 service with 99% of users routed through a healthy region and 1% on the degraded region has high severity (the degraded region is in bad shape) but low blast radius (1% of users affected). A tier-2 service that is moderately degraded but in the critical path for a key revenue journey may have moderate severity and high blast radius. The two measurements are independent inputs to the response policy, and conflating them is one of the most common ways teams misjudge the urgency of an incident.

Note
Blast radius is not severity. Severity asks “how bad?”; blast radius asks “how broadly?” A 50%-degraded service affecting 2% of users is a different problem from a 5%-degraded service affecting 90% of users. The first invites careful mitigation of a narrow blast surface; the second invites broader action even though the per-user severity is lower. The two questions are independent. The architecture treats them as independent inputs.

The Business Impact Agent computes blast radius from artefacts the Signal Plane already maintains. The topology graph tells it which downstream consumers traverse the degraded path. The Observability Agent’s traffic-distribution signals tell it what proportion of requests are exposed. The ownership metadata tells it which user cohorts are affected (geography, customer tier, journey type). The contract metadata tells it which SLAs are implicated. The output is a structured blast-radius object attached to the situational-awareness object, with each contributing input traceable:

  blast_radius:
	affected_traffic_pct: 12.4
	affected_cohorts:
  	- geography: "eu-west"
    	traffic_share: 0.68
  	- customer_tier: "enterprise"
    	accounts_implicated: 3
    	sla_at_risk_at: "30m_continuous_degradation"
  	- journey: "checkout_submit"
    	critical: true
	spread_velocity: "stable"
    estimated_business_impact_per_hour_gbp: 18000
    estimated_business_impact_confidence: 0.71
	computed_at: "t=28s"
Notice the confidence on the business-impact estimate. The Business Impact Agent does not pretend to know the financial impact precisely. It computes the estimate from traffic patterns, observed conversion rates, and historical revenue-per-completed-journey, and it reports the calibrated confidence of that estimate alongside the number. The Governance Plane’s policy can choose to use the estimate or to ignore it depending on the confidence band. Policies for tier-1 actions often require the business-impact estimate to exceed a threshold with sufficient confidence; below that, the action is treated as if blast radius alone is the input. This is what allows the Business Impact Agent to participate in routine incidents without overcommitting to financial precision that nobody can actually defend.

The other property the Business Impact Agent surfaces is spread velocity. A blast radius of 12% that is stable is a different operational situation from a blast radius of 12% that is doubling every two minutes. The first invites measured response; the second invites immediate containment regardless of severity, because the projected blast radius in five minutes is what actually drives the urgency. The Business Impact Agent computes velocity from the time-series of affected traffic and surfaces it explicitly. The Reasoning Plane uses velocity as one input when ranking candidate actions in section 5.5: containment actions move up the ranking when velocity is high; investigative actions move up when velocity is stable and the cost of getting the action wrong outweighs the cost of waiting.

To make this concrete one more time against the canonical scenario. At t=28s in the payment-latency incident, the Business Impact Agent’s assessment: affected traffic 12% and rising, EU-west evening peak, three enterprise accounts in the affected cohort, estimated revenue impact £18k/hr with confidence 0.71, spread velocity slow but positive. The Reasoning Plane reads this alongside the leading hypothesis (dependency saturation, confidence 0.82) and the causal timeline (payment-gateway deploy 23 minutes ago, causal likelihood 0.74). The picture is now coherent enough for the Reasoning Plane to generate concrete candidate actions, which is what section 5.5 develops. Before that, what the architecture has done is translate a latency spike on a graph into a structured assessment of cause, blast radius, business impact, and spread velocity, each with calibrated confidence, available for downstream reasoning, in roughly twenty-five seconds. The human equivalent of this assembly, in 2026, runs to ten minutes on a good day.

5.5 Recommendation Generation and Action Planning
With detection done, timeline assembled, blast radius estimated, the Reasoning Plane has enough to do the work it was designed for. It generates the candidate action set: the small list of plausible responses to the situation, each scored against the same dimensions, each attached to a concrete action contract, each carrying its own confidence and reversal path. The candidate action set is the central artefact of the Reason beat of DRAL, and it is what the Governance Plane will evaluate before anything is executed. This section is the Reason beat at full resolution.

The mental shift worth naming is that the Reasoning Plane does not select a single action. It surfaces a set. Experienced incident commanders do this naturally: they consider rollback against throttling against scaling, they weigh the trade-offs aloud, they pick the option whose worst case they understand best. The agent does the same work, but it does it explicitly and with the trade-offs scored against measurable dimensions. The output is not “the system has decided to do X.” It is “the system has identified three candidates; this is what each one looks like; this is which one we recommend and why.” The Governance Plane reads the set, applies policy, and authorises whichever option (if any) clears the policy bar. The candidate action set is the structured artefact that makes this possible.

Each candidate carries five fields. The action contract declares what the action does, what it requires, how it reverses, and what counts as success (the format from Chapter 4.5). The confidence score declares how strongly the Reasoning Plane believes this action will resolve the leading hypothesis. The predicted impact declares the expected blast-radius and SLO effects. The reversal path declares how the action can be undone if it does not work. The governance level declares the policy regime the action falls under, which is what the Governance Plane downstream uses to decide whether the action is permitted at the current autonomy boundary. These five fields are the minimum a candidate must declare to be a useful input to the Governance Plane. Less than this, and the policy cannot evaluate the proposal. More than this, and the format becomes unwieldy without adding decision-relevant information.

EXAMPLE
Three candidate actions for the canonical scenario, surfaced by the Reasoning Plane at t=30 seconds. Leading hypothesis: dependency saturation on payment-gateway, confidence 0.82.

  candidate_1:
    action_contract: throttle_non_critical_paths
	target: checkout-api
	scope: non-critical request classes, 40% throttle, 10min max
	confidence: 0.87
    predicted_impact:
      latency_p99_recovery: 4-6min
      affected_traffic_pct: 8 (subset of currently-affected 12%)
      revenue_impact_offset_gbp: -3000 (cost of throttling)
	reversal: automatic_on_signal_recovery
    governance_level: autonomous_above_0.80
  candidate_2:
    action_contract: rollback_deploy
	target: payment-gateway
	scope: revert to release rel-payment-gateway-12846 (prior known-good)
	confidence: 0.71
    predicted_impact:
      latency_p99_recovery: 8-12min
      affected_traffic_pct: 12 (full current blast)
      side_effect_risk: 0.18 (rollback may introduce regression
                          	if other dependent services have caught up)
	reversal: rollback_of_rollback (re-deploy current release)
    governance_level: requires_human_approval_for_tier1
  candidate_3:
    action_contract: scale_payment_gateway
	target: payment-gateway
	scope: +3 replicas in eu-west, 15min duration
	confidence: 0.42
    predicted_impact:
      latency_p99_recovery: unknown (does not address root cause)
      cost_increase_gbp_per_hour: 240
      success_probability_if_cause_correct: 0.55
	reversal: automatic_scale_down_after_window
    governance_level: autonomous_above_0.80
The three candidates are not equivalent. The first is high-confidence containment, low blast, fast effect, fully autonomous under current policy. The second is the textbook root-cause fix, but it carries higher operational risk, longer time to effect, and policy requires human approval before a tier-1 service is rolled back. The third is the wrong action for the leading hypothesis (scaling won’t help dependency saturation, only capacity exhaustion), and the confidence score reflects this honestly. The Reasoning Plane is not embarrassed to surface a low-confidence option. The candidate’s purpose is to give the Governance Plane and the on-the-loop human the full picture, including the option that probably won’t help.

The recommended action emerges from ranking. The Reasoning Plane scores each candidate against the four dimensions experienced incident commanders weight implicitly: effectiveness (how likely this action is to resolve the situation), risk (what new failure modes the action could introduce), reversibility (how easily the action can be undone if wrong), and time to effect (how quickly the action will measurably change system behaviour). The four dimensions are not equally weighted in every situation. During a fast-spreading incident, time to effect dominates. During a slow-burn incident, risk and reversibility dominate. The Reasoning Plane’s ranking accounts for this, surfacing the action whose dimension-weighted score is highest given the current blast-radius velocity and the leading hypothesis’s confidence.

![image](https://hackmd.io/_uploads/r1UNEGp-Gg.png)

Figure 7-1. Three candidate actions for the canonical scenario. The agent surfaces all three; the policy authorises one.

In the worked example, the ranking would put candidate_1 (throttle) at the top. It clears autonomous-execution policy, it is fully reversible automatically, it addresses the leading hypothesis with high confidence, and it produces an effect within minutes. Candidate_2 (rollback) is more effective if the leading hypothesis is correct, but its higher risk, slower effect, and policy gate make it the right choice only if the throttle fails or confidence rises further. Candidate_3 (scale) is the wrong action; the Reasoning Plane surfaces it for transparency but does not recommend it. The Governance Plane reads the ranking and approves candidate_1 autonomously. The Execution Plane runs the action. The audit trail records the full candidate set, the ranking, the policy decision, and the chosen action. If later evidence proves the leading hypothesis was wrong, the Learning Agent has the full reasoning trail to refine future rankings.

The discipline worth flagging is that the candidate set is the unit of audit, not the chosen action. When a human later asks “why did the agent throttle traffic at 03:00,” the answer is not “the agent decided to throttle.” The answer is “the agent considered three candidates, ranked them according to this policy, surfaced this trade-off, and the policy authorised the highest-ranked option.” The reasoning is reconstructable. The alternatives that were considered and rejected are visible. The policy that determined the choice is inspectable. This is what makes autonomous incident response defensible to leadership, to compliance, and to the team itself. Chapter 9 will develop the trade-off modelling that makes this ranking robust in detail. Chapter 11 will develop the Decision Budget that bounds how much autonomy the architecture allows over time. For incident response, what matters is that the candidate action set exists, is structured, is auditable, and is the input the Governance Plane evaluates against policy before anything happens to the system.

5.6 Autonomous Execution Within Accountability Boundaries
The candidate action has been ranked. The Governance Plane has evaluated it against policy. Authority to execute is granted. The Act beat of DRAL is now ready to fire, and what happens in the next seconds is what every reader has been waiting to see: an autonomous system executing a real action against a real service, with the architectural safeguards from Chapter 4 doing their actual job. This section is the Act beat at full resolution, and it is also where the authority tier concept (formal treatment in Chapter 11.4) makes its first appearance.

Autonomous execution is not unrestricted action. It is the precise opposite: action whose scope, duration, reversibility, and outcome verification are all declared in advance, executed by a system that cannot exceed those declarations. The action contract from Chapter 4.5 is what makes the execution bounded. The Governance Plane’s policy is what authorises this specific action under these specific conditions. The Execution Plane is what runs the contract within the bounds the policy approved. The architecture’s behaviour is predictable because every step is structured, and the autonomy itself is what the team designed when they wrote the action contract and the policy. The agent is not making it up. The team made the design decisions. The agent is executing them.

The authority tier is the granular concept that makes governance graduated rather than binary. Rather than asking “is this action allowed,” the Governance Plane asks “at what tier is this action allowed right now.” The tiers, named here as a preview of the formal treatment in Chapter 11.4:

Read-only operations
The agent observes and reports but does not change system state. Always allowed.

Containment operations
The agent reduces blast radius without irreversible state changes. Examples: throttling, isolating, draining. Typically autonomous above a confidence threshold for the service tier.

Corrective operations
The agent changes system state in ways that are automatically reversible. Examples: scaling, restarting, feature-flag toggles, controlled rollbacks. Typically autonomous above a higher confidence threshold, with reversal mandatory.

Structural operations
The agent changes system state in ways that are irreversible or affect a large blast surface. Examples: schema migrations, cross-region failover, data repairs. Always requires human approval at the time of execution.

The tier is not a property of the action alone; it is a property of the action in the current context. A throttle that affects 8% of traffic is a containment action; the same throttle that affects 80% of traffic crosses into corrective territory and triggers tighter policy. A scale-out that adds three replicas is corrective; a scale-out that doubles a service’s footprint moves toward structural and requires elevated authority. The Governance Plane reads the action contract’s declared scope, computes the effective tier given the current context, and matches it to the policy for that tier. The autonomy boundary moves dynamically with the situation, which is what allows the same architecture to be aggressive during low-risk operations and conservative during high-risk ones without changing the underlying mechanism.

The execution itself follows a sequence the rest of the book will reference back to:

  execution_sequence:
	1. revalidate:
   	- signal contracts still current
   	- confidence still above policy threshold
   	- blast radius assessment still within bounds
   	- no contradicting evidence has arrived since proposal
	2. acquire_scoped_identity:
   	- workload identity for this specific action
   	- least-privilege credentials
   	- audit-trail attribution
	3. execute_per_contract:
   	- precondition checks pass
   	- action runs within declared scope
   	- duration bounded by contract maximum
	4. observe_outcome:
   	- watch the action's declared outcome signals
   	- watchdog enforces success-criteria evaluation
   	- emit outcome signals back to Signal Plane
	5. evaluate_completion:
   	- did the action achieve declared success criteria
   	- within declared time window
   	- without triggering declared abort conditions
	6. reversal_or_completion:
   	- if successful: action stays in effect for declared duration, then reverses automatically
   	- if unsuccessful: reversal fires immediately, audit event emitted, escalation triggered
This is not optional ceremony. Each step is what allows the autonomy to be defensible. Revalidation prevents stale-context execution (between t=22s when the policy approved and t=23s when the executor starts, a contradicting signal may have arrived). Scoped identity prevents over-broad privilege; the action runs with credentials sufficient for this action and no more. Contract-bound execution prevents scope creep; the action does what the contract declares and nothing else. Outcome observation closes the DRAL Learn beat; without it, the system would act but not learn. Evaluation and reversal-on-failure prevent action-without-effect; the agent does not declare success because the action ran, it declares success because the action achieved the declared outcome.

Warning
An executed action without a recorded rationale is not an autonomous action. It is an unattributed one. The audit trail is the architectural feature that makes autonomy defensible; without it, the team cannot answer “why did the system do X” except with “the agent decided to,” which is not an answer that survives leadership review or regulatory inquiry. Every action runs with its candidate-action context, its policy decision, its scoped identity, and its outcome observation attached. Anything less is automation pretending to be intelligence.

The canonical scenario at t=30s through t=215s. The Governance Plane authorises throttle_non_critical_paths under the autonomous_throttling_policy_v2.0. The Execution Plane acquires scoped identity (workload identity tied to the platform team for this specific service), runs the contract’s preconditions one more time (all still pass), and updates the traffic-classifier configuration to throttle the secondary and background path classes by 40% on checkout-api. The action_started event emits at t=31s. The Signal Plane begins observing the action’s effect. The watchdog watches the declared success criteria: latency_p99 returns to baseline within 5 minutes, error rate stays under 1%. At t=120s, the watchdog reports preliminary success: p99 has dropped 60% of the way back to baseline. At t=215s, full success: p99 is at baseline, error rate stable, throttled request count was 1,247 (small absolute number). The action is now in its automatic-reversal window; throttling will lift in 7 more minutes unless the situation reasserts itself. The audit trail records every step. The team’s on-the-loop engineer was notified at t=15s when the incident was detected, was notified again at t=30s when autonomous action was authorised, and will receive the post-incident summary when the incident is closed. They did not need to take any action. They watched the architecture do its job, and the SLO timeline confirmed the work was sound.

![image](https://hackmd.io/_uploads/rkqHNMpWze.png)

Figure 7-2. The DRAL loop applied to the canonical scenario, beat by beat, from detection through learning.


A worse case is worth tracing too. Suppose at t=120s the watchdog reports that the throttling did not produce the expected recovery. p99 has not moved. The architecture does several things automatically. The throttle is held (it is not making things worse, and removing it might). The Reasoning Plane is signalled that its leading hypothesis may be wrong; it re-ranks the candidate set with the new evidence. Confidence on the dependency-saturation hypothesis drops because the action that should have helped did not. Confidence on the inventory-api change hypothesis rises modestly. The Governance Plane is now in a different policy regime: with the leading hypothesis confidence below the autonomous threshold, the next action requires human approval. The on-call engineer is paged with the full context: what was tried, what happened, what the agent now believes, what the alternative candidates are. The agent does not escalate authority on its own; it escalates uncertainty to the human, with the full reasoning trail attached. This is what bounded autonomy looks like in practice: the agent knows when to stop.

5.7 Human in the Loop in Incident Response
Chapter 4.7 named the three modes of human-in-the-loop: above the loop, in the loop, on the loop. This section applies that framework specifically to incident response. The substance of the three modes does not change. What changes is the operational tempo, because incidents collapse the cadence of all three modes onto a few minutes.

Above the loop in incident response is the work that happened before the incident started. The team’s intent declarations (what counts as steady state for this service), the policies they wrote (which actions are autonomous under which conditions), the action contracts they catalogued (what the agent is allowed to do), the SLO thresholds they set. None of this is happening during the incident. All of it is operating during the incident. The above-the-loop human’s contribution to the incident is the work they did weeks or months earlier, and the quality of that contribution is what determines whether the architecture can handle the incident autonomously at all. A team that under-invested in above-the-loop work will discover it during the incident, when the policy turns out to be missing the case the incident represents, and the agent escalates because no policy allows the action it would otherwise have taken.

In the loop in incident response is what happens when the Governance Plane routes a proposal to a human. The canonical case: a candidate action whose confidence falls below the policy’s autonomous threshold, or whose authority tier requires human approval by design (the rollback case from section 5.5). The in-the-loop human is not paging in cold. They are receiving a candidate action with full context: the leading hypothesis, the alternatives, the evidence, the recommended action, the policy reason the decision was routed to them. Their decision is fast (seconds to a minute) because the assembly work is already done. They approve, modify, or reject. The Execution Plane runs whatever they approved. The architecture continues. The human’s value here is judgement applied to a well-prepared question, not assembly applied to raw evidence. This is what makes in-the-loop participation high-value rather than draining at 03:00.

On the loop in incident response is the engineer watching the architecture handle the incident autonomously, ready to intervene if something looks off but not required to approve anything. They see the audit trail accumulating in real time. They see the SLO indicators (the five flagships from section 3.6) trending. They can interrogate the situational-awareness object directly if they want to. They can also do nothing, and the architecture will handle the incident from detection through learning without their input. This is the dominant mode at maturity Level 3 and above. Most incidents do not need an in-the-loop human; the on-the-loop human is sufficient.

The canonical scenario at three different maturity levels demonstrates the shifts. At L2 (Augmented), every proposed action routes to a human. The agent does the assembly work (situational awareness, timeline, blast radius, candidate set), but execution requires explicit human approval. The on-call engineer is in the loop continuously. At L3 (Bounded), low-risk actions execute autonomously under policy; high-risk actions still route. The on-call engineer is in the loop intermittently, depending on what the incident demands. At L4 (Adaptive), most actions execute autonomously; the human is on the loop, watching, intervening rarely. The same incident has a different human signature at each level, and section 5.10 makes that comparison concrete.

What does not change across levels is accountability. The team that owns the service owns the outcome, regardless of whether a human in the loop pressed approve. The architecture moves execution. It does not move responsibility. The on-call engineer at L4 is not less responsible for what the system did just because they did not type a command. They are responsible because they (or their team) wrote the policy that allowed the agent to act. Section 3.7’s accountability boundary is the architectural artefact that holds this distinction stable. Section 4.7 named the three modes. This section just shows what they look like under incident pressure.

5.8 Post-Incident Learning and Feedback Loops
The incident closes. The throttling reverses. The latency stabilises. In a traditional operation, this is where the playbook ends: write a postmortem, assign action items, schedule a review meeting, move on. The Learning Agent makes its first full appearance in this section because the architecture’s view of what happens after an incident is fundamentally different. Learning is not a document that gets written. It is a structured update to the system’s models of itself, driven by the same loop that handled the incident, captured automatically, applied through governed paths. The Learn beat of DRAL closes here.

The Learning Agent is the role inside the Reasoning Plane that owns this loop, and its job is narrow and important: turn every incident into measurable updates to the system’s models, in a way the rest of the architecture can absorb safely. Specifically, the Learning Agent updates four kinds of artefacts. Similarity stores (the case base of “incidents that looked like this one”) receive a new entry with the leading hypothesis, the action taken, and the outcome attached. Confidence calibration models update based on whether the stated confidences turned out to match the observed success rates. Policy parameters receive proposed adjustments (not changes, proposals) when the data suggests current thresholds are too tight or too loose. Action-effectiveness ratings update for each action contract, based on whether the action achieved its declared success criteria. The Calibration Error SLO from section 3.6 is the canary that tells the team whether the Learning Agent’s updates are actually improving the system. If CE rises after a learning update, the update has corrupted the calibration and should be rolled back.

The Closed Learning Loop (CLL) is the architectural pattern this section introduces formally, and the book will reference it throughout the later chapters. The pattern has five stages. Capture what the system believed during the incident (situational awareness, leading hypothesis, candidate set, chosen action, predicted outcome). Observe what actually happened (achieved outcome, side effects, recovery time, secondary incidents triggered). Compare expectation to outcome (where did the prediction match, where did it diverge, what does the divergence imply about the underlying model). Propose updates to the system’s models, parameters, and policies based on the comparison. Apply the updates through governed paths, with automatic application for low-risk updates and human review required for high-risk ones. Each stage runs automatically. The human’s role in the loop is to review the proposed updates, not to produce them.

To make this concrete, the Learning Agent’s update payload for the canonical scenario would include something like:

  incident_learning:
	incident_id: INC-2026-05-13-CHECKOUT-LATENCY
	leading_hypothesis: dependency_saturation:payment-gateway
	chosen_action: throttle_non_critical_paths
	predicted_outcome:
  	latency_p99_recovery: 4-6min
  	effectiveness: 0.87
	observed_outcome:
  	latency_p99_recovery: 3min_20sec
  	effectiveness: 0.92
  	side_effects: none
	learning_signals:
  	- signal: effectiveness_underestimated_for_dependency_saturation
    	delta: +0.05
    	applies_to: throttle_non_critical_paths action contract
    	proposed_update: action_effectiveness_rating +5%
    	risk: low
    	path: auto_apply_with_review
  	- signal: calibration_within_band
    	delta: 0.02
    	applies_to: dependency_saturation hypothesis class
    	proposed_update: none required
    	path: log_only
  	- signal: payment_gateway_deploy_correlation_strengthened
    	delta: +0.08
    	applies_to: causal_likelihood scoring for this pattern
    	proposed_update: weight_increase_for_payment_gateway_recent_deploys
    	risk: medium
    	path: human_review_required
	similarity_store_entry:
  	hash: <content_addressed>
  	retrieval_keys:
    	- signal_pattern: latency_p99_drift_with_upstream_degradation
    	- service_tier: tier_1
    	- upstream_service: payment-gateway
    	- resolution_strategy: throttle_non_critical_paths
The first learning signal (effectiveness was underestimated) is low-risk and auto-applies with review: the action-effectiveness rating for this contract goes up modestly, the agent will reach for this action with marginally higher confidence next time. The second is a no-op (calibration was within band, no update needed; the signal is logged for trend monitoring). The third is medium-risk: increasing the causal-likelihood weight for payment-gateway recent deploys would change how the agent ranks candidate causes in similar future incidents, which is a structural change to reasoning that the on-the-loop human reviews before applying. The Learning Agent does not bypass this gate. It surfaces the proposal with the evidence attached, and the human approves or rejects. The audit trail records both the proposal and the decision.

Note
An incident that does not update the Learning Agent’s models is an incident the system will face again, identically. The Learn beat is not optional. It is what makes the architecture compound over time rather than asymptote at its initial level. A team that handles incidents brilliantly but skips the Learn beat will discover, three quarters in, that their second hundred incidents are no easier than their first hundred. A team that runs the Learn beat religiously will discover that the same class of incident becomes less frequent, less impactful, or faster to resolve every quarter. The compounding curve from section 3.0 is what the Learn beat produces.

The other property worth naming is that learning is additive across the recurring cast. The Observability Agent updates its signal contracts based on what was missing or noisy during the incident. The Topology Agent updates the dependency graph based on what relationships became visible during the incident. The Release Agent updates its causal-likelihood scoring for changes of the kind that mattered. The Business Impact Agent updates its translation models based on how well its predicted impact matched the observed business effect. The Learning Agent coordinates these updates, ensuring that no single agent’s update corrupts another’s calibration, and that the system’s models remain coherent across agents. This is what allows the architecture to operate at L5 (Systemic), where coordinated learning across agents is what produces the compounding curve. At L3 or L4, the Learning Agent operates in a simpler mode: it owns the learning artefacts for the Reasoning Plane and surfaces proposals for the other agents’ owners to apply through their own review paths. The architecture’s behaviour is correct at all levels; the speed of compounding rises with maturity.

What this section does not claim is that postmortems disappear. They do not. Postmortems remain valuable as the human-readable narrative of what happened, and they remain the primary artefact for organisational learning across teams. What changes is that the postmortem is no longer the only place learning lives. The system-level learning is captured automatically by the Learning Agent in a form the system can act on. The postmortem captures the human-level learning in a form the team can act on. The two are complementary, and together they produce the kind of resilience curve that makes ARE worth the engineering investment.

5.9 Measuring Incident Response in an Agentic World
The five flagship SLOs from section 3.6 are how incident response is measured in an agentic architecture. This section does not introduce new metrics. It shows what the five look like during a real incident timeline, which is what makes them operationally meaningful rather than abstract. The numbers in this section are illustrative; the operating bands a team would actually use will vary by service tier and maturity level.

Autonomous Resolution Rate (ARR) is the rolling percentage of detected incidents that the architecture resolves end-to-end without human escalation. For incident response specifically, ARR is segmented by severity and service tier. A team at L3 maturity on tier-1 services might run at ARR 35% on tier-1 (the high-risk cases that legitimately require human-in-the-loop participation), ARR 70% on tier-2 (most can be handled autonomously), and ARR 90%+ on tier-3 (routine degradations the architecture catches and handles before anyone notices). The canonical scenario, ending at t=215s with the throttle holding and recovery confirmed, counts as one autonomous resolution. The team’s tier-1 ARR over the past 30 days is the rolling aggregate of decisions like this one.

Decision Quality SLO (DQ-SLO) measures whether the agent’s chosen actions were the right ones. A decision is successful if steady state is restored within the declared window, recovery constraints are not violated, and no follow-on incident is triggered. In the canonical scenario, the chosen action (throttle) restored p99 within the predicted window with no side effects: a successful decision. The Reasoning Plane’s leading hypothesis (dependency saturation) was correct: a successful diagnosis. Both contribute to the DQ-SLO numerator. If the throttle had not worked and the rollback that the team eventually approved had introduced a secondary degradation, both decisions would have counted against DQ-SLO. A team at L3 maturity on tier-1 services should be running DQ-SLO above 80% to justify autonomy at that tier; below that band, the policy should tighten until DQ-SLO recovers.

Reasoning Latency SLO (RL-SLO) measures how long the architecture takes to move from initial signal to committed, policy-compliant action. The canonical scenario: detection at t=15s, candidate action ranked at t=30s, action committed at t=31s. RL-SLO for this incident: 16 seconds. The team’s p95 RL-SLO target for tier-1 incidents at L3 maturity might be 45 seconds; p99 might be 90 seconds. The numbers vary by domain. What matters is that the team has declared the band and is operating against it. Long reasoning latencies usually trace to one of three causes: inefficient investigation paths in the Reasoning Plane, excessive tool calls during context assembly, or unclear policies that require back-and-forth with the Governance Plane. Each is fixable, and RL-SLO is the metric that surfaces which one is dominant.

Action Effectiveness SLO (AE-SLO) measures whether the actions actually achieved their intended outcome without collateral damage. The throttle in the canonical scenario achieved declared success criteria, did not breach cost or blast-radius constraints, and did not trigger a secondary incident: AE-SLO numerator. A throttle that had restored p99 latency but tripled the team’s monthly cloud bill (because it triggered an unintended cascade of retries elsewhere) would have failed AE-SLO even if the immediate outcome looked good. AE-SLO is the metric that makes the cost of agentic recovery legible to the business in the same way availability is.

Calibration Error (CE) measures the gap between the agent’s stated confidence and its actual success rate. If the Reasoning Plane consistently reports 0.85 confidence for decisions that succeed only 60% of the time, CE rises and the team should tighten the autonomy threshold. If 0.85-confidence decisions actually succeed 87% of the time, the calibration is sound. The Learning Agent’s coordination of confidence calibration across the four agents (section 5.8) is what keeps CE low. A team that watches CE catches drift months before it shows up in user-visible metrics. For incident response specifically, CE is the SLO that flags whether the agent’s increasing autonomy is justified by improving reasoning quality or is being earned through threshold inflation. The first is healthy. The second is the L2 → L3 Trust Ceiling failure mode from section 4.9.

EXAMPLE
The five flagships measured against the canonical scenario:

  scenario: payment-gateway dependency saturation, checkout latency
  detection_time: t=15s
  resolution_time: t=215s (3m 20s)
  per-incident measurements:
	ARR contribution: +1 (this incident resolved autonomously)
	DQ-SLO contribution: +1 (action succeeded, no secondary incident)
	RL-SLO: 16 seconds (signal to commit)
	AE-SLO contribution: +1 (success criteria met, no side effects)
	CE contribution:
      stated_confidence_at_decision: 0.82 (dependency saturation hypothesis)
      observed_outcome: success
      contributes_to_0.80-0.85_confidence_bin
  rolling 30-day SLOs for tier-1 services at L3 maturity:
	ARR: 36%  	(target: 20-40%)
	DQ-SLO: 84%   (target: ≥80%)
	RL-SLO p95: 38s  (target: ≤45s)
	AE-SLO: 87%   (target: ≥80%)
	CE: 14%   	(target: ≤25%)
interpretation: the architecture is operating within its declared L3 band.

ARR could rise further as confidence in this class of failure improves.

CE is well below the L3 threshold, supporting eventual L4 promotion.

The architectural property that makes the five flagships useful is that they are emitted by the same Signal Plane the rest of the architecture runs on. They are not a separate measurement system. The audit trail of every incident already contains everything needed to compute them: the candidate action set, the chosen action, the policy decision, the outcome observation, the leading hypothesis and its confidence. The Learning Agent’s per-incident summary (the payload from section 5.8) is the data each flagship is computed from. There is no separate reporting step. The metrics are a consequence of the architecture, not an addition to it.

![image](https://hackmd.io/_uploads/BJxPNfp-Gx.png)

Figure 7-3. The five flagship SLOs measured against the canonical scenario, against the 30-day band for tier-1 services at L3 maturity.

This is also why the five flagships are appropriate for every workflow in Part II, not just incident response. Chapter 6 will use them to measure delivery quality. Chapter 7 will use them to measure chaos engineering’s learning velocity. Chapter 8 will use them to measure pre-incident work’s effect on production incident rate. The same five SLOs, the same architecture, different workflows. This is what makes the agentic operating model coherent across the surface area of reliability work, which is what Part IV will develop.

5.10 One Incident, Five Maturity Levels
The same incident, played at all five maturity levels. The failure does not change. The triggering conditions do not change. The blast radius is identical. The only variable is what the architecture can do, and what the team has to do to compensate for what the architecture cannot. This is the chapter’s set piece, and it is the cleanest demonstration in the book of what agentic reliability actually buys.

The incident: at 14:23 GMT on a Tuesday, the payment-gateway service in eu-west enters a degraded state due to a deploy that landed 23 minutes earlier. Latency on the dependent checkout-api begins to drift upward. The signal trajectory is the same across all five levels; what differs is what happens between t=0 and resolution.

Level 1: Reactive
The team operates on dashboards, alerts, and runbooks. At t=2 minutes, the page-duty system fires when error-rate thresholds finally cross on the checkout-api. The on-call engineer opens five tabs: the metrics dashboard, the trace explorer, the deploy log, the Slack channel for the on-call group, and the topology diagram on the wiki (last updated four months ago). They begin assembling context manually. At t=12 minutes, they identify that payment-gateway deployed 35 minutes earlier, but they do not yet know whether that is correlation or cause. At t=18 minutes, they reach out to the payment-gateway team on Slack; that team is asleep (eu-west evening, but the team is in the US Pacific zone). At t=24 minutes, they decide to throttle non-critical paths on checkout-api manually; they spend three minutes finding the right runbook command. At t=27 minutes, the throttle is applied. At t=32 minutes, latency begins to recover. At t=38 minutes, the incident is closed. Total time: 38 minutes. Time the human spent assembling context: 22 minutes. Number of dashboards consulted: 8. Number of decisions the human made under pressure: roughly 14. Post-incident learning: a postmortem will be written, an action item will be assigned, a wiki page will be updated.

Level 2: Augmented
Same incident. At t=0:15 the Observability Agent emits a structured latency signal with full context attached. The Topology Agent attaches the dependency graph. The Release Agent attaches recent change events. The situational-awareness object lands at t=0:18. The Reasoning Plane generates three candidate actions, ranks them, attaches confidence scores. At t=0:30 the agent’s recommendation lands in the on-call engineer’s pager: “Recommend throttle_non_critical_paths on checkout-api. Leading hypothesis: dependency saturation on payment-gateway. Confidence 0.82. Predicted recovery 4-6 minutes. Two alternatives attached.” The engineer reviews the recommendation. The assembly is done; they are not staring at five dashboards. They approve at t=1:10. The action runs. Latency recovers by t=5 minutes. Incident closed at t=8 minutes. Total time: 8 minutes. Time the human spent assembling context: zero. Time the human spent making the decision: 40 seconds. Suggestion Acceptance Rate for this category of recommendation: 87% (the agent’s advice is consistently sound, which is what L2 has to demonstrate before L3 is safe).

Level 3: Bounded
Same incident. The situational-awareness object lands at t=0:18, same as L2. The Reasoning Plane generates the candidate set, same as L2. At t=0:30 the Governance Plane evaluates the leading candidate against policy. Confidence 0.82 exceeds the autonomous-execution threshold of 0.80 for this service tier under dependency-degradation conditions. The action runs without human approval. The on-call engineer is notified at t=0:31 (“autonomous action executed: throttle_non_critical_paths; reasoning attached”) and watches the recovery from the audit-trail UI. They take no action; the architecture handles the incident. Latency recovers by t=4 minutes. Incident closed at t=5 minutes. Total time: 5 minutes. The on-call engineer was on the loop, not in the loop. ARR contribution: +1. The trust ceiling has been crossed: the architecture is now agent-as-executor, not agent-as-advisor, and the team has the Governance Plane and the action contracts and the calibrated confidence to make that safe.

Level 4: Adaptive
Same incident. Everything from L3, plus the architecture has been operating long enough that the Learning Agent’s calibration on this failure class is sharper. The Reasoning Plane’s hypothesis confidence is 0.89 (higher than at L3, because past incidents of this signature have refined the model). The Governance Plane’s policy is itself parameterised by current SLO state: because CE is running at 11% (well below the 25% threshold), the autonomous-execution band has widened, and tier-1 dependency-saturation actions can now be taken at confidence 0.75 rather than 0.80. The action runs at t=0:27. Latency recovers by t=3 minutes. Incident closed at t=4 minutes. Total time: 4 minutes. The architecture is now adapting its own thresholds to its observed calibration, within the bounds the team’s policy declared. The on-call engineer’s involvement during the incident: zero. Their above-the-loop work has been substantial; they spent three afternoons last quarter refining the policy and the action contracts, and that work is paying off here.

Level 5: Systemic
Same incident, except at L5 the incident often does not happen at the same scale. The Topology Agent, watching payment-gateway’s pre-deploy signals on the morning of the change, flagged that the deploy carried a higher-than-usual risk profile (one of the changed components had a history of latency-sensitive interactions). The Release Agent attached this flag to the deploy event. The Governance Plane’s policy for tier-1 dependency deploys with elevated risk profile required a smaller initial canary cohort. The deploy was canaried to 5% of payment-gateway capacity for the first 30 minutes before progressing. The latency degradation surfaced at the canary stage, affecting <1% of checkout traffic instead of 12%. The agent throttled the canary’s traffic to its parent service, ARR-credited an autonomous resolution at minute 1, and the Release Agent rolled the canary back to the prior known-good. Total customer-visible impact: under 60 seconds, affecting <1% of users. The agent ecosystem learned: the Learning Agent updated the Release Agent’s canary-sizing model for this class of deploy. The architecture caught its own future failure mode before the failure was material. This is what L5 actually buys: not faster response, but fewer incidents at material scale, because the architecture is increasingly able to anticipate.

EXAMPLE
The same incident at each level, compared:

L1	L2	L3	L4	L5
Total time	38 min	8 min	5 min	4 min	~60s
Customer-visible	~25%	~12%	~8%	~6%	1%
Human in-the-loop	Yes	Yes (40s)	No	No	No
Human on-the-loop	N/A	N/A	Yes	Yes	Minimal
ARR contribution	0	0	+1	+1	+1
Above-the-loop work	Postmortem	Postmortem	Policy	Policy	Predictive
+ contracts + risk profiles			
Time to next	Possibly	Likely	Less	Rarer	Caught
similar incident	repeat	similar	likely	still	pre-incident
The structural pattern: the gain from L1 to L2 is mostly about elimination of assembly. The gain from L2 to L3 is the crossing of the Trust Ceiling. The gain from L3 to L4 is adaptive calibration. The gain from L4 to L5 is anticipation and ecosystem coordination. Each transition has its own characteristic work, and each transition costs roughly a quarter of focused engineering investment to achieve.

The point of this comparison is not that L5 is the goal for every team. Most teams should be operating somewhere in the L2-to-L3 range for the next three years, and L3 for tier-1 services is a reasonable steady-state. The point is that each level is reachable, the transitions are legible, and the value at each level is measurable. A team that does not know which level they are at, and which level they could safely move to, is a team that will either over-invest in agentic infrastructure without the substrate to absorb it, or under-invest and continue to pay the human cost of incident response that the architecture could have handled. The five flagship SLOs from section 5.9 are how a team locates themselves. The maturity model from Chapter 4.9 is what tells them what each level looks like. This chapter, all the way through, is the worked picture of what L3 incident response actually feels like in production.

![image](https://hackmd.io/_uploads/ryRPVGa-zl.png)

Figure 7-4. The same incident at each of the five maturity levels. The chapter’s set piece. The Trust Ceiling at the L2 → L3 transition is what every adoption effort has to plan for.
A second observation worth drawing from this comparison concerns where the gains come from. The L1-to-L2 gain (38 minutes to 8 minutes) is almost entirely the elimination of context assembly. The same human is making the same decision, but they are doing it on top of an agent’s prepared work rather than from raw evidence. The L2-to-L3 gain (8 minutes to 5 minutes) is the elimination of the human’s approval step for actions that policy already authorised; the structural risk this absorbs is what the Trust Ceiling represents. The L3-to-L4 gain (5 minutes to 4 minutes) is adaptive calibration; the architecture’s own thresholds tighten and loosen with its observed performance, and the policy reflects what the system has earned rather than what was provisioned. The L4-to-L5 gain (4 minutes to under one) is anticipation; the architecture catches the failure at the canary stage, before the full blast radius materialises. Each transition has its own characteristic engineering work, and the work is not the same at every transition. Teams that try to skip a transition’s characteristic work will discover, at the next incident, exactly which engineering substrate they did not invest in.

A third observation is that the chart hides a discipline that every level depends on: the team’s investment in their own above-the-loop work. At L1, the team’s above-the-loop work is mostly runbooks and dashboards, which the on-call engineer consumes during the incident. At L2, the above-the-loop work begins to include the signal-emission discipline and the topology graph maintenance that the agents will read from. At L3 and beyond, the above-the-loop work is the policy itself, the action contracts, the autonomy thresholds, the SLO bands. The amount of human time spent on agentic reliability does not decrease as maturity rises. It moves earlier in the lifecycle. The team is not less involved at L4 than at L1. They are involved differently. The above-the-loop work is what makes the in-incident hands-off behaviour safe, and the discipline of that work is what scales a team’s reliability efforts.

5.11 From Response to Design
Incident response is where agentic reliability proves itself, and the proof is not subtle. The same incident handled by an L1 team and an L3 architecture differs by a factor of seven in time to resolution, an order of magnitude in customer-visible impact, and a categorical difference in what the team has to do while the incident is in motion. The architecture is not magic. It is the result of deliberate engineering investment in the four planes, the action contracts, the policies, and the agentic SLOs. The investment compounds, which is what makes it different from automation. Automation reduces a fixed cost. Agentic reliability builds a curve that bends down across quarters, as the Learning Agent’s models sharpen and the Calibration Error stays low and the team’s confidence in expanding autonomy is earned rather than asserted.

What this chapter has not claimed is that incidents disappear. They do not. The L5 picture in section 5.10 is not zero incidents; it is incidents caught earlier, contained smaller, and resolved by the architecture rather than by exhausted humans at 03:00. The cost the team pays for this is not zero either. It is the above-the-loop work that has to happen before the incident, the policy design and the action contract authoring and the SLO band tuning and the calibration review. The total engineering effort is comparable to what teams already spend on reliability; what changes is where the effort lands in time and what kind of work it is. The reactive heroics of incident response shrink. The proactive structural work grows. The total stays roughly constant, but its character shifts in ways that make the team’s working life materially better.

Chapter 6 is the next workflow. If incident response is where the architecture proves itself under pressure, delivery is where the same architecture pays back the investment without pressure at all. Most reliability failures begin not during incidents but during change, and the same four planes that handle a degradation in production can handle a degradation in a deploy if the architecture is wired to evaluate change against the same machinery. The Release Agent makes its full debut. The Change Risk Score (CRS) makes its formal introduction. The cleanest worked formula in the book lives in 6.3, and the discipline it encodes is the same discipline this chapter has been developing: structured artefacts, calibrated confidence, governed action, learned outcomes. Different workflow. Same architecture. The book is what compounds.