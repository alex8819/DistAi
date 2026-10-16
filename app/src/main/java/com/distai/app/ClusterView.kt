package com.distai.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.cos
import kotlin.math.sin

/**
 * Anello del modello distribuito: un arco per ogni telefono, largo quanto la sua parte di modello (MB reali del piano).
 * Il movimento è la realtà: le particelle scorrono solo sui nodi il cui worker sta calcolando (CPU misurata), più veloci
 * quanto più è alta; un nodo fermo o senza dato (cpu < 0) resta fermo.
 */
class ClusterView(ctx: Context, attrs: AttributeSet? = null) : View(ctx, attrs) {
    data class Seg(val name: String, val mb: Long, val layers: Int, val cpu: Int, val color: Int)

    private var segs = emptyList<Seg>()
    private var tokS = 0.0
    private var title = ""
    private val arc = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeCap = Paint.Cap.BUTT }
    private val dot = Paint(Paint.ANTI_ALIAS_FLAG)
    private val txt = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
    private val box = RectF()
    private val phase = FloatArray(16)
    private var last = 0L

    private val ringH get() = dp(250f)
    private val rowH get() = dp(46f)

    fun set(s: List<Seg>, tok: Double, t: String) {
        val resize = s.size != segs.size
        segs = s; tokS = tok; title = t
        if (resize) requestLayout()
        invalidate()
    }

    override fun onMeasure(wSpec: Int, hSpec: Int) {
        setMeasuredDimension(MeasureSpec.getSize(wSpec), (ringH + rowH * segs.size + dp(64f)).toInt())
    }

    private fun dp(v: Float) = v * resources.displayMetrics.density

    override fun onDraw(c: Canvas) {
        val now = System.nanoTime()
        val dt = if (last == 0L) 0f else ((now - last) / 1e9f).coerceAtMost(0.1f)
        last = now
        val w = width.toFloat(); val h = ringH
        val cx = w / 2; val cy = h / 2
        val stroke = dp(22f)
        val r = minOf(w, h) / 2 - stroke
        box.set(cx - r, cy - r, cx + r, cy + r)
        arc.strokeWidth = stroke
        arc.color = 0xFFE3E8F0.toInt()
        c.drawCircle(cx, cy, r, arc)
        val total = segs.sumOf { it.mb }.coerceAtLeast(1)
        var a = -90f
        var anim = false
        segs.forEachIndexed { i, s ->
            val sweep = 360f * s.mb / total
            val gap = if (segs.size > 1) 2f else 0f
            val live = s.cpu > 0
            arc.color = s.color
            arc.alpha = if (live) 255 else 150
            c.drawArc(box, a + gap / 2, (sweep - gap).coerceAtLeast(0.5f), false, arc)
            if (live) {
                // particelle chiare sull'arco: velocità proporzionale alla CPU misurata
                anim = true
                phase[i % 16] = (phase[i % 16] + dt * (0.15f + s.cpu / 100f * 0.85f)) % 1f
                dot.color = 0xFFFFFFFF.toInt()
                for (k in 0 until 3) {
                    val f = (phase[i % 16] + k / 3f) % 1f
                    val ang = Math.toRadians((a + gap / 2 + (sweep - gap) * f).toDouble())
                    c.drawCircle(cx + r * cos(ang).toFloat(), cy + r * sin(ang).toFloat(), stroke * 0.22f, dot)
                }
            }
            a += sweep
        }
        txt.color = 0xFF1B2A41.toInt()
        txt.textSize = dp(26f); txt.isFakeBoldText = true
        c.drawText(if (total > 1) "%.1f GB".format(total / 1024.0) else "—", cx, cy + dp(2f), txt)
        txt.textSize = dp(12f); txt.isFakeBoldText = false; txt.color = 0xFF6B7A90.toInt()
        c.drawText(title, cx, cy + dp(22f), txt)
        c.drawText(Lang.t("modello diviso tra i telefoni", "model split across the phones"), cx, h - dp(2f), txt)
                drawLegend(c, w, ringH)
        if (anim) postInvalidateOnAnimation() else last = 0L
    }

    /** Sotto l'anello: per ogni telefono quota di modello e uso CPU misurato; in fondo la velocità del cluster. */
    private fun drawLegend(c: Canvas, w: Float, top: Float) {
        val pad = dp(8f)
        segs.forEachIndexed { i, s ->
            val y = top + rowH * i
            dot.color = s.color
            c.drawCircle(pad + dp(7f), y + dp(14f), dp(6f), dot)
            txt.textAlign = Paint.Align.LEFT; txt.isFakeBoldText = true; txt.textSize = dp(15f); txt.color = 0xFF1B2A41.toInt()
            c.drawText(s.name, pad + dp(22f), y + dp(19f), txt)
            txt.isFakeBoldText = false; txt.textSize = dp(13f); txt.color = 0xFF6B7A90.toInt()
            txt.textAlign = Paint.Align.RIGHT
            val share = "%.1f GB".format(s.mb / 1024.0) + if (s.layers > 0) " · ${s.layers} layer" else ""
            c.drawText(share, w - pad, y + dp(19f), txt)
            // barra CPU: vuota se il telefono non calcola, "n.d." se il dato non c'è
            val bx = pad + dp(22f); val bw = w - bx - pad - dp(56f); val by = y + dp(30f)
            arc.style = Paint.Style.FILL; arc.color = 0xFFE3E8F0.toInt(); arc.alpha = 255
            c.drawRoundRect(bx, by, bx + bw, by + dp(8f), dp(4f), dp(4f), arc)
            if (s.cpu > 0) { arc.color = s.color; c.drawRoundRect(bx, by, bx + bw * s.cpu / 100f, by + dp(8f), dp(4f), dp(4f), arc) }
            arc.style = Paint.Style.STROKE
            txt.textAlign = Paint.Align.RIGHT; txt.textSize = dp(12f)
            c.drawText(if (s.cpu < 0) Lang.t("CPU n.d.", "CPU n/a") else "CPU ${s.cpu}%", w - pad, by + dp(9f), txt)
        }
        txt.textAlign = Paint.Align.CENTER; txt.isFakeBoldText = true; txt.textSize = dp(22f); txt.color = 0xFF1B2A41.toInt()
        c.drawText(if (tokS > 0) "⚡ %.1f tok/s".format(tokS) else "⚡ — tok/s", w / 2, top + rowH * segs.size + dp(34f), txt)
        txt.isFakeBoldText = false; txt.textAlign = Paint.Align.CENTER
    }
}
