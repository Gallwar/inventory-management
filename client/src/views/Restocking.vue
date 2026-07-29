<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div class="card budget-card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.availableBudget') }}</h3>
        </div>
        <div class="budget-value">{{ formattedBudget }}</div>
        <div class="budget-control">
          <span class="budget-bound">{{ formattedBudgetMin }}</span>
          <input
            type="range"
            class="budget-slider"
            v-model.number="budget"
            :min="budgetMin"
            :max="budgetMax"
            :step="budgetStep"
            :aria-label="t('restocking.availableBudget')"
          />
          <span class="budget-bound">{{ formattedBudgetMax }}</span>
        </div>
        <p class="budget-hint">{{ t('restocking.budgetHint') }}</p>
        <p class="full-restock-note">
          {{ t('restocking.fullRestockCost') }}: <strong>{{ formattedFullRestockCost }}</strong>
        </p>
      </div>

      <div class="stats-grid restocking-stats-grid">
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.selectedCost') }}</div>
          <div class="stat-value">{{ formattedSelectedCost }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ formattedRemainingBudget }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.itemsSelected') }}</div>
          <div class="stat-value">{{ itemsSelectedLabel }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.totalUnits') }}</div>
          <div class="stat-value">{{ formattedTotalUnits }}</div>
        </div>
        <div class="stat-card">
          <!-- Lead time is a neutral fact, not a warning - no colour modifier -->
          <div class="stat-label">{{ t('restocking.longestLeadTime') }}</div>
          <div class="stat-value">{{ formattedLongestLeadTime }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendations') }}</h3>
        </div>
        <!-- Keep the last-known table visible (dimmed) while a debounced reload is in flight -->
        <div class="table-container" :class="{ 'is-refreshing': refreshing }">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.itemName') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th>{{ t('restocking.table.required') }}</th>
                <th>{{ t('restocking.table.orderQuantity') }}</th>
                <th>{{ t('restocking.table.unitCost') }}</th>
                <th>{{ t('restocking.table.lineCost') }}</th>
                <th>{{ t('restocking.table.supplier') }}</th>
                <th>{{ t('restocking.table.leadTime') }}</th>
                <th>{{ t('restocking.table.status') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in recommendations"
                :key="item.item_sku"
                :class="{ 'row-skipped': !item.selected }"
              >
                <td><strong>{{ item.item_sku }}</strong></td>
                <td>{{ translateProductName(item.item_name) }}</td>
                <td>
                  <span :class="['badge', item.trend]">{{ trendLabel(item.trend) }}</span>
                </td>
                <td>{{ item.required_quantity.toLocaleString() }}</td>
                <td>
                  {{ item.order_quantity.toLocaleString() }}
                  <span class="pack-detail">{{ packDetail(item) }}</span>
                </td>
                <td>{{ formatCurrencyWithDecimals(item.unit_cost ?? 0, currentCurrency, 2) }}</td>
                <td>{{ formatCurrency(item.line_cost ?? 0, currentCurrency) }}</td>
                <td>{{ item.supplier }}</td>
                <td>{{ t('restocking.leadTimeDays', { days: item.lead_time_days }) }}</td>
                <td>
                  <span :class="['badge', statusBadgeClass(item)]">{{ statusLabel(item) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="order-action">
        <div v-if="orderConfirmation" class="confirmation-banner">
          <p>{{ t('restocking.orderPlaced', orderConfirmation) }}</p>
          <router-link to="/orders" class="view-link">{{ t('restocking.viewInOrders') }}</router-link>
        </div>
        <p v-else-if="itemsSelectedCount === 0" class="nothing-affordable">
          {{ t('restocking.nothingAffordable') }}
        </p>
        <button v-else class="place-order-btn" :disabled="submitting" @click="placeOrder">
          {{ submitting ? t('restocking.placingOrder') : t('restocking.placeOrder') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency, formatCurrencyWithDecimals } from '../utils/currency'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, currentLocale, translateProductName } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    // True only while a debounced re-fetch (not the initial load) is in flight
    const refreshing = ref(false)
    const submitting = ref(false)

    // Whole API response kept together so budget bounds/default travel with the data
    const data = ref(null)
    // Slider position; adopts the server-derived default once the first response arrives
    const budget = ref(0)
    const orderConfirmation = ref(null)

    // Plain closure variables (not reactive) for debounce bookkeeping
    let debounceTimer = null
    // Suppresses the watcher's fetch the one time we set `budget` programmatically from the server default
    let suppressNextFetch = false

    const recommendations = computed(() => data.value?.recommendations ?? [])
    const selectedItems = computed(() => recommendations.value.filter(item => item.selected))

    const budgetMin = computed(() => data.value?.budget_min ?? 0)
    const budgetMax = computed(() => data.value?.budget_max ?? 0)
    const budgetStep = computed(() => data.value?.budget_step ?? 1000)

    const formattedBudget = computed(() => formatCurrency(budget.value, currentCurrency.value))
    const formattedBudgetMin = computed(() => formatCurrency(budgetMin.value, currentCurrency.value))
    const formattedBudgetMax = computed(() => formatCurrency(budgetMax.value, currentCurrency.value))
    const formattedFullRestockCost = computed(() => formatCurrency(data.value?.full_restock_cost ?? 0, currentCurrency.value))

    const formattedSelectedCost = computed(() => formatCurrency(data.value?.total_cost ?? 0, currentCurrency.value))
    // Clamp at zero: the selection algorithm should never exceed budget, but guard against rounding drift
    const remainingBudgetAmount = computed(() => Math.max(0, budget.value - (data.value?.total_cost ?? 0)))
    const formattedRemainingBudget = computed(() => formatCurrency(remainingBudgetAmount.value, currentCurrency.value))
    const itemsSelectedCount = computed(() => data.value?.items_selected ?? 0)
    const itemsSelectedLabel = computed(() => `${itemsSelectedCount.value} / ${data.value?.items_total ?? 0}`)
    const formattedTotalUnits = computed(() => (data.value?.total_units ?? 0).toLocaleString())

    // Longest lead time among selected items only - skipped items don't affect delivery timing
    const longestLeadTime = computed(() => {
      if (selectedItems.value.length === 0) return 0
      return Math.max(...selectedItems.value.map(item => item.lead_time_days))
    })
    const formattedLongestLeadTime = computed(() => {
      return longestLeadTime.value > 0 ? t('restocking.leadTimeDays', { days: longestLeadTime.value }) : '—'
    })

    const formatDate = (dateString) => {
      const date = new Date(dateString)
      if (isNaN(date.getTime())) return dateString
      const locale = currentLocale.value === 'ja' ? 'ja-JP' : 'en-US'
      return date.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' })
    }

    // --- Per-row helpers ---

    const trendLabel = (trend) => t(`trends.${trend.toLowerCase()}`)

    const packDetail = (item) => {
      // Guard against a missing/zero pack size instead of dividing by zero
      if (!item.pack_size) return ''
      return t('restocking.packDetail', { packs: item.order_quantity / item.pack_size, size: item.pack_size })
    }

    const statusLabel = (item) => {
      if (item.selected) return t('restocking.included')
      if (item.reason === 'over_budget') return t('restocking.overBudget')
      if (item.reason === 'no_catalog_entry') return t('restocking.noCatalogEntry')
      return item.reason
    }

    const statusBadgeClass = (item) => {
      if (item.selected) return 'success'
      if (item.reason === 'over_budget') return 'warning'
      return 'danger'
    }

    // --- Data loading ---

    const loadRecommendations = async (budgetValue, { initial = false } = {}) => {
      if (initial) {
        loading.value = true
      } else {
        refreshing.value = true
      }
      error.value = null
      try {
        const response = await api.getRestockingRecommendations(budgetValue)
        data.value = response
        if (initial) {
          // Adopt the server-derived default as the slider's starting position. Assigning
          // `budget` here would normally re-trigger the watcher below for data we already
          // have, so we suppress that one fetch.
          suppressNextFetch = true
          budget.value = response.budget_default
        }
      } catch (err) {
        error.value = 'Failed to load restocking recommendations: ' + err.message
      } finally {
        loading.value = false
        refreshing.value = false
      }
    }

    watch(budget, () => {
      // A budget change makes any prior order confirmation stale
      orderConfirmation.value = null

      if (suppressNextFetch) {
        suppressNextFetch = false
        return
      }

      // Debounce slider drags so we don't fire a request per pixel of movement
      if (debounceTimer) clearTimeout(debounceTimer)
      debounceTimer = setTimeout(() => {
        loadRecommendations(budget.value)
      }, 250)
    })

    const placeOrder = async () => {
      submitting.value = true
      error.value = null
      try {
        const payload = {
          budget: budget.value,
          items: selectedItems.value.map(item => ({
            item_sku: item.item_sku,
            item_name: item.item_name,
            quantity: item.order_quantity,
            unit_cost: item.unit_cost,
            supplier: item.supplier,
            lead_time_days: item.lead_time_days
          }))
        }
        const response = await api.createPurchaseOrder(payload)
        orderConfirmation.value = {
          poNumber: response.po_number,
          items: response.items.length,
          cost: formatCurrency(response.total_cost, currentCurrency.value),
          date: formatDate(response.expected_delivery)
        }
        // Refresh recommendations so the table reflects the post-order state
        await loadRecommendations(budget.value)
      } catch (err) {
        error.value = 'Failed to place order: ' + err.message
      } finally {
        submitting.value = false
      }
    }

    onMounted(() => loadRecommendations(undefined, { initial: true }))

    onUnmounted(() => {
      if (debounceTimer) clearTimeout(debounceTimer)
    })

    return {
      t,
      loading,
      error,
      refreshing,
      submitting,
      budget,
      budgetMin,
      budgetMax,
      budgetStep,
      formattedBudget,
      formattedBudgetMin,
      formattedBudgetMax,
      formattedFullRestockCost,
      formattedSelectedCost,
      formattedRemainingBudget,
      itemsSelectedCount,
      itemsSelectedLabel,
      formattedTotalUnits,
      formattedLongestLeadTime,
      recommendations,
      orderConfirmation,
      placeOrder,
      trendLabel,
      packDetail,
      statusLabel,
      statusBadgeClass,
      translateProductName,
      formatCurrencyWithDecimals,
      formatCurrency,
      currentCurrency
    }
  }
}
</script>

<style scoped>
/* Override the global 4-col .stats-grid so all 5 cards fit one row on wide
   viewports instead of leaving the 5th card alone on a mostly-empty second row. */
.restocking-stats-grid {
  grid-template-columns: repeat(5, 1fr);
}

/* Below ~1200px, five equal columns get too cramped - fall back to auto-fit
   so cards wrap sensibly instead of squeezing. */
@media (max-width: 1200px) {
  .restocking-stats-grid {
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  }
}

.budget-card {
  text-align: center;
}

.budget-value {
  font-size: 2.5rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
  margin-bottom: 1rem;
}

.budget-control {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  max-width: 640px;
  margin: 0 auto;
}

.budget-bound {
  font-size: 0.813rem;
  color: #64748b;
  flex-shrink: 0;
  min-width: 60px;
}

.budget-bound:last-child {
  text-align: right;
}

.budget-slider {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: #e2e8f0;
  appearance: none;
  outline: none;
  cursor: pointer;
}

.budget-slider::-webkit-slider-thumb {
  appearance: none;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #3b82f6;
  cursor: pointer;
  border: 2px solid white;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.budget-slider::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #3b82f6;
  cursor: pointer;
  border: 2px solid white;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.budget-hint {
  color: #64748b;
  font-size: 0.875rem;
  margin-top: 0.875rem;
}

.full-restock-note {
  color: #64748b;
  font-size: 0.875rem;
  margin-top: 0.375rem;
}

.full-restock-note strong {
  color: #0f172a;
}

.table-container {
  transition: opacity 0.15s ease;
}

.table-container.is-refreshing {
  opacity: 0.55;
}

/* Skipped candidates recede visually but stay legible and stay in the table */
tbody tr.row-skipped {
  color: #94a3b8;
  background: #f8fafc;
}

tbody tr.row-skipped td {
  color: #94a3b8;
  font-weight: 400;
}

tbody tr.row-skipped strong {
  color: #94a3b8;
  font-weight: 500;
}

.pack-detail {
  display: block;
  color: #94a3b8;
  font-size: 0.75rem;
  margin-top: 0.125rem;
}

.order-action {
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 1rem 0;
}

.place-order-btn {
  background: #3b82f6;
  color: white;
  border: none;
  padding: 0.75rem 2rem;
  border-radius: 8px;
  font-size: 0.938rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s ease;
}

.place-order-btn:hover:not(:disabled) {
  background: #2563eb;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.nothing-affordable {
  color: #64748b;
  font-size: 0.938rem;
  text-align: center;
}

.confirmation-banner {
  background: #d1fae5;
  border: 1px solid #a7f3d0;
  color: #065f46;
  padding: 1rem 1.25rem;
  border-radius: 8px;
  text-align: center;
  width: 100%;
}

.confirmation-banner p {
  margin-bottom: 0.5rem;
}

.confirmation-banner .view-link {
  color: #065f46;
  font-weight: 600;
  text-decoration: underline;
}
</style>
