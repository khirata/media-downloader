package com.mediadownloader.publisher

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.mediadownloader.publisher.databinding.ActivityShareBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

class ShareActivity : AppCompatActivity() {

    private lateinit var binding: ActivityShareBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityShareBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val sharedUrl = extractUrl(intent)
        if (sharedUrl == null) {
            Toast.makeText(this, getString(R.string.no_url_found), Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        binding.urlText.text = sharedUrl

        binding.publishButton.setOnClickListener {
            val description = binding.descriptionInput.text.toString().trim()
            publish(sharedUrl, description.ifEmpty { null })
        }

        binding.cancelButton.setOnClickListener { finish() }

        binding.settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
    }

    private fun extractUrl(intent: Intent): String? {
        if (intent.action != Intent.ACTION_SEND) return null
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return null
        // Extract the first http/https URL from the shared text
        val urlRegex = Regex("""https?://\S+""")
        return urlRegex.find(text)?.value ?: text.trim().takeIf { it.startsWith("http") }
    }

    private fun publish(url: String, description: String?) {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        val endpoint = prefs.getString(PREF_ENDPOINT, "")
        val apiKey = prefs.getString(PREF_API_KEY, "")

        if (endpoint.isNullOrBlank()) {
            Toast.makeText(this, getString(R.string.configure_endpoint), Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }

        setUiEnabled(false)
        binding.statusText.visibility = View.VISIBLE
        binding.statusText.text = getString(R.string.publishing)

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                sendToApi(endpoint, apiKey, url, description)
            }

            if (result.success) {
                Toast.makeText(this@ShareActivity, getString(R.string.publish_success), Toast.LENGTH_SHORT).show()
                finish()
            } else {
                binding.statusText.text = getString(R.string.publish_failed, result.error)
                setUiEnabled(true)
            }
        }
    }

    private fun setUiEnabled(enabled: Boolean) {
        binding.publishButton.isEnabled = enabled
        binding.cancelButton.isEnabled = enabled
        binding.descriptionInput.isEnabled = enabled
    }

    data class ApiResult(val success: Boolean, val error: String? = null)

    private fun sendToApi(endpoint: String, apiKey: String?, url: String, description: String?): ApiResult {
        return try {
            val connection = URL(endpoint).openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            if (!apiKey.isNullOrBlank()) {
                connection.setRequestProperty("x-api-key", apiKey)
            }
            connection.doOutput = true
            connection.connectTimeout = 10_000
            connection.readTimeout = 15_000

            val body = JSONObject().apply {
                put("urls", JSONArray().apply { put(url) })
                if (description != null) put("description", description)
            }

            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val code = connection.responseCode
            if (code in 200..299) {
                ApiResult(success = true)
            } else {
                val error = connection.errorStream?.bufferedReader()?.readText() ?: "HTTP $code"
                ApiResult(success = false, error = "HTTP $code: $error")
            }
        } catch (e: Exception) {
            ApiResult(success = false, error = e.message)
        }
    }

    companion object {
        const val PREFS_NAME = "media_downloader_prefs"
        const val PREF_ENDPOINT = "endpoint"
        const val PREF_API_KEY = "api_key"
    }
}
