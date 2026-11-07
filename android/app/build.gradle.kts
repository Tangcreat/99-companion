plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.ninety.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.ninety.companion"
        minSdk = 24
        targetSdk = 34
        versionCode = 8
        versionName = "1.7"

        // 只打 arm64：现在几乎全是这个架构，且能把包体积砍掉一半
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // python/ 目录不当资源压缩，Chaquopy 直接读源码
    androidResources {
        noCompress += listOf("imy", "so")
    }
}

chaquopy {
    defaultConfig {
        // Chaquopy 16.x 支持的 Python 版本范围是 3.8~3.12
        version = "3.11"
        pip {
            install("requests")
            install("pillow")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.core:core-ktx:1.12.0")
}
