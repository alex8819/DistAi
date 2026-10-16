package com.distai.app

import android.content.Context

/** Lingua dell'interfaccia (parte in italiano): scelta con le bandierine in Home e ricordata. */
object Lang {
    @Volatile var it = true
    fun load(c: Context) { it = c.getSharedPreferences("distai", Context.MODE_PRIVATE).getBoolean("lang_it", true) }
    fun save(c: Context, italian: Boolean) {
        it = italian
        c.getSharedPreferences("distai", Context.MODE_PRIVATE).edit().putBoolean("lang_it", italian).apply()
    }
    fun t(italian: String, english: String) = if (it) italian else english
}
