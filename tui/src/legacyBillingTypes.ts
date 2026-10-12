/** Wire types retained for imported presentation components.
 * The AstralNexus Gateway does not implement billing RPCs; these types do not
 * advertise or enable purchases. Kept locally rather than depending on a
 * missing sibling application or weakening the compiler with `any`.
 */
export interface BillingErrorPayload { ok: boolean; error?: string; message?: string; portal_url?: string | null }
export interface BillingCardInfo { brand?: string; last4?: string; masked: string; display?: string; resolved_via?: string }
export interface BillingAutoReload {
  enabled: boolean; threshold_usd?: string; reload_to_usd?: string
  threshold_display?: string; reload_to_display?: string
  card: { kind: string; masked?: string; brand?: string; last4?: string; display?: string }
}
export interface BillingMonthlyCap {
  limit_usd: string | null; limit_display: string; spent_display: string; is_default_ceiling?: boolean
}
export interface UsageBarData {
  fill_fraction: number; pct_used?: number | null; remaining_display: string; total_display?: string
}
export interface UsageModelData {
  available: boolean; status?: string; plan_name?: string | null; renews_display?: string
  total_spendable_display?: string; has_topup?: boolean
  plan_bar?: UsageBarData | null; topup_bar?: UsageBarData | null
}
export interface BillingStateResponse extends BillingErrorPayload {
  logged_in: boolean; is_admin: boolean; can_charge: boolean; cli_billing_enabled: boolean
  org_name?: string; role?: string; balance_display: string; balance_usd: string
  min_usd: string; max_usd: string; charge_presets: string[]; charge_presets_display: string[]
  card: BillingCardInfo | null; auto_reload: BillingAutoReload | null; monthly_cap: BillingMonthlyCap | null
  usage?: UsageModelData
}
export interface BillingMutationResponse extends BillingErrorPayload {}
export interface BillingChargeResponse extends BillingErrorPayload { status?: string; charge_id?: string }
export interface BillingChargeStatusResponse extends BillingChargeResponse {}
export interface SubscriptionTierOption {
  tier_id: string; name: string; tier_order: number; dollars_per_month_display: string
  monthly_credits: string; is_current: boolean; is_enabled: boolean
}
export interface SubscriptionCurrent {
  tier_id: string; tier_name: string; monthly_credits?: string; credits_remaining?: string
  cycle_ends_at?: string | null; pending_downgrade_tier_name?: string | null
  pending_downgrade_at?: string | null; pending_downgrade_tier_id?: string | null
  cancel_at_period_end?: boolean; cancellation_effective_display?: string; cancellation_effective_at?: string | null
  pending_downgrade_display?: string
}
export interface SubscriptionStateResponse extends BillingErrorPayload {
  logged_in: boolean; is_admin: boolean; can_change_plan: boolean; org_name?: string; org_id?: string
  role?: string; current: SubscriptionCurrent | null; tiers: SubscriptionTierOption[]; usage?: UsageModelData
  context?: string
}
export interface SubscriptionPreviewResponse extends BillingErrorPayload {
  effect?: string; target_tier_name?: string; charge_now_cents?: number; monthly_credits_delta?: string
  effective_at?: string | null; reason?: string
  amount_due_now_cents?: number
}
export interface SubscriptionUpgradeResponse extends BillingErrorPayload {
  reason?: string; status?: string; recovery_url?: string | null; target_tier_name?: string
}
