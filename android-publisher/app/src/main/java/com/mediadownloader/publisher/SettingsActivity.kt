package com.mediadownloader.publisher

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.mediadownloader.publisher.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val prefs = getSharedPreferences(ShareActivity.PREFS_NAME, MODE_PRIVATE)

        // Load saved values
        binding.endpointInput.setText(prefs.getString(ShareActivity.PREF_ENDPOINT, ""))
        binding.apiKeyInput.setText(prefs.getString(ShareActivity.PREF_API_KEY, ""))

        binding.saveButton.setOnClickListener {
            val endpoint = binding.endpointInput.text.toString().trim()
            val apiKey = binding.apiKeyInput.text.toString().trim()

            if (endpoint.isBlank()) {
                binding.endpointLayout.error = getString(R.string.endpoint_required)
                return@setOnClickListener
            }
            if (!endpoint.startsWith("https://") && !endpoint.startsWith("http://")) {
                binding.endpointLayout.error = getString(R.string.endpoint_invalid)
                return@setOnClickListener
            }

            binding.endpointLayout.error = null
            prefs.edit()
                .putString(ShareActivity.PREF_ENDPOINT, endpoint)
                .putString(ShareActivity.PREF_API_KEY, apiKey)
                .apply()

            Toast.makeText(this, getString(R.string.settings_saved), Toast.LENGTH_SHORT).show()
        }
    }
}
